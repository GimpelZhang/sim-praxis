"""Phase 2 Task8 C 线 CLI/工厂/评估/Gate1/Gate2 接线测（攢批第 74 条；0 付费）。

覆盖（全部零网络/零容器/零 Genesis 实跑，硬规则 10；fake 产物只落 tmp_path）：
  1. CLI 组合守卫：--variant fluid 仅 task=8；--stage0-report 仅 variant（exit 2）；
  2. 出生/回收双态工厂：内容层无 track2_questions/03_kitchen_pour → 出生合同
     注入 + cmd 指向出生时不存在的入口；有 → resolve+stage（清单损坏 fail-closed
     不回退出生）；同 run 重入不重复注入合同；D-5 零 RunSpec；
  3. evaluate 两级装入：state 键 → telemetry 同目录 → results.json+trajectory.npz
     引擎组装（85% 入杯中位/守恒机判引擎现算、衍生自报字段 fail-closed、
     9 字段冻结、窗内曲线 CSV、可重放不重写）；
  4. Gate1 接缝：sph_ok/pbd_ok → 真人卡方法（Stage0 流体能力事实随卡）；
     both_fail → D-5 零卡片 GraphStateError；client 重挂不重发；
  5. 卡片结构：2.0/1.0 Stage0 插页 + Gate2 流体指标分支不破旧路径。
"""
from __future__ import annotations

import json
import stat
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

from orchestrator.graph import cli as graph_cli
from orchestrator.graph import task8_fluid as t8f
from orchestrator.graph.nodes import GraphStateError
from orchestrator.solutions import SolutionError

THREAD = "t8f-wiring"


# ---------------------------------------------------------------------------
# 合成 Stage0 流体能力报告（hermetic；与 test_task8_fluid_seam 同构）
# ---------------------------------------------------------------------------

def _good_s0(**overrides) -> dict:
    report = {
        "schema": t8f.STAGE0_REPORT_SCHEMA,
        "ok": True,
        "outcome": "sph_ok",
        "reflected_materials": {"top": ["Weird"], "nested": {"Weird": ["Liquid"]}},
        "attempts": [{
            "api_used": "Weird.Liquid",
            "n_particles": 1000,
            "steps": t8f.STAGE0_STEPS,
            "completed_steps": t8f.STAGE0_STEPS,
            "finite_coords": True,
            "passed": True,
            "vram_abort": False,
            "position_accessor": "get_particles_pos",
            "vram_peak_gb": 1.2,
            "reductions": 0,
            "failure_verbatim": None,
        }],
        "gpu": {"name": "Fake A800", "baseline_mib": 300.0, "peak_mib": 1228.8},
        "versions": {"python": "3.10.99", "genesis": "1.3.3", "torch": "2.4.1"},
        "ts_utc": "2026-10-26T00:00:00Z",
        "wall_s": 12.3,
    }
    report.update(overrides)
    return report


def _both_fail_s0() -> dict:
    r = _good_s0(ok=False, outcome="both_fail")
    r["attempts"][0].update(
        passed=False, finite_coords=False, completed_steps=0,
        failure_verbatim="SPH exploded: NaN positions")
    return r


def _write_s0(tmp_path: Path, doc: dict | None = None) -> Path:
    p = tmp_path / "stage0" / t8f.STAGE0_REPORT_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc or _good_s0(), ensure_ascii=False),
                 encoding="utf-8")
    return p


def _state(s0_path: Path, **extra) -> dict:
    state = {
        "thread_id": THREAD,
        "track": "track2",
        "proposals": [{"name": "Q5 厨房倒水最小闭环",
                       "sim_engine": "Genesis 粒子流体",
                       "first_principles": "严格几何入杯 + 守恒三查",
                       "artifacts": ["video.mp4"]}],
        "chosen": {"name": "Q5 厨房倒水最小闭环",
                   t8f.STAGE0_REPORT_STATE_KEY: str(s0_path)},
        "events": [],
    }
    state.update(extra)
    return state


def _factory_env(tmp_path: Path, monkeypatch, s0_path: Path) -> dict:
    empty_kb = tmp_path / "kb_solutions"
    empty_kb.mkdir(exist_ok=True)
    monkeypatch.setattr(t8f, "solutions_root", lambda: empty_kb)
    return _state(s0_path)


# ---------------------------------------------------------------------------
# 合成 Stage1 产物（results.json + trajectory.npz）
# ---------------------------------------------------------------------------

CUP = {"inner_radius": 0.1, "bottom_z": 0.0, "rim_z": 0.2}
FRAME_DT = 0.02
N = 100


def _frame(n_in: int, n_splash: int, n_outside: int, n_below: int) -> np.ndarray:
    """按严格几何语义放置 n_in/n_splash/n_outside/n_below 粒子的一帧。"""
    pts: list[list[float]] = []
    pts += [[0.0, 0.0, 0.1]] * n_in            # r<0.1, 0<z<0.2 → in_cup
    pts += [[0.0, 0.0, 0.3]] * n_splash        # z>=rim → splash
    pts += [[0.15, 0.0, 0.1]] * n_outside      # r>=inner, mid z → outside_wall
    pts += [[0.0, 0.0, -0.01]] * n_below       # z<=bottom → below_bottom
    assert len(pts) == N, f"{len(pts)} != {N}"
    return np.asarray(pts, dtype=np.float32)


def _trajectory(*, oob_outside: int = 1) -> np.ndarray:
    """5 帧全飞溅 + 25 帧稳态（oob_outside 越界 / 其余入杯+飞溅+杯底下）。

    末 0.5s 窗（25 帧 @ dt=0.02）逐帧恒定，故中位数=每帧值：
    oob_outside=1 → 90 入杯 / 8 飞溅 / 1 越界 / 1 杯底下（入杯 0.90、溢出 0.02，
    守恒通过 0.02≤0.02）；oob_outside=5 → 86 入杯 / 8 飞溅 / 5 越界 / 1 杯底下
    （入杯 0.86、溢出 0.06>0.02，守恒违例——违例是结果不是异常，组装仍成功）。
    """
    n_below = 1
    n_splash = 8
    n_in = N - oob_outside - n_below - n_splash
    assert n_in > 0
    early = np.stack([_frame(0, N, 0, 0) for _ in range(5)])
    steady = np.stack([
        _frame(n_in, n_splash, oob_outside, n_below) for _ in range(25)])
    return np.concatenate([early, steady], axis=0)


def _write_stage1_artifacts(tmp_path: Path, *, oob_outside: int = 1,
                            video: bool = True,
                            manifest_override: dict | None = None,
                            results_override: dict | None = None) -> Path:
    wd = tmp_path / "run_1"
    wd.mkdir(parents=True, exist_ok=True)
    results = {
        "schema": t8f.prompts.RESULTS_SCHEMA,
        "solver_api": "SPH.Liquid",
        "n_particles": N,
        "frame_dt_s": FRAME_DT,
        "cup": CUP,
    }
    if results_override:
        results.update(results_override)
    (wd / t8f.RESULTS_NAME).write_text(
        json.dumps(results, ensure_ascii=False), encoding="utf-8")
    positions = _trajectory(oob_outside=oob_outside)
    np.savez_compressed(wd / t8f.TRAJECTORY_NAME, positions=positions)
    if video:
        (wd / t8f.VIDEO_NAME).write_bytes(b"\x00fake-mp4")
    (wd / t8f.COVER_NAME).write_bytes(b"\x89PNGfake")
    man = {"n_frames": int(positions.shape[0]), "frame_dt_s": FRAME_DT}
    if manifest_override:
        man.update(manifest_override)
    (wd / t8f.MANIFEST_NAME).write_text(
        json.dumps(man, ensure_ascii=False), encoding="utf-8")
    return wd


# ---------------------------------------------------------------------------
# ① CLI 组合守卫
# ---------------------------------------------------------------------------

def test_cli_variant_validation_exit_codes():
    with mock.patch.object(graph_cli, "_invoke", return_value=0) as inv:
        # --variant fluid 非 task8 → exit 2
        rc = graph_cli.main(["run", "--thread", "t", "--track", "track2",
                             "--question", "Q5", "--batch-id", "b1",
                             "--task", "7", "--variant", "fluid"])
        assert rc == 2
        inv.assert_not_called()
        # --variant libero 配 task8 → exit 2
        rc = graph_cli.main(["run", "--thread", "t", "--track", "track2",
                             "--question", "Q5", "--batch-id", "b1",
                             "--task", "8", "--variant", "libero"])
        assert rc == 2
        inv.assert_not_called()
        # --stage0-report 无 variant → exit 2
        rc = graph_cli.main(["run", "--thread", "t", "--track", "track2",
                             "--question", "Q5", "--batch-id", "b1",
                             "--task", "8", "--stage0-report", "/x"])
        assert rc == 2
        inv.assert_not_called()
        # 合法组合进 _invoke（task=8 + variant fluid + coding 批次）
        rc = graph_cli.main(["run", "--thread", "t8f-ok", "--track", "track2",
                             "--question", "Q5 厨房倒水", "--batch-id", "b1",
                             "--task", "8", "--variant", "fluid",
                             "--coding-batch-id", "b2"])
        assert rc == 0
        inv.assert_called_once()


def test_cli_initial_state_injects_stage0(tmp_path):
    s0 = _write_s0(tmp_path)
    args = graph_cli.argparse.Namespace(
        thread="t8f-inject", track="track2", question="Q5", index=None,
        variant="fluid", stage0_report=str(s0))
    state = graph_cli._initial_state(args, {"question": "Q5 厨房倒水"})
    assert state[t8f.STAGE0_REPORT_STATE_KEY] == str(s0)
    args2 = graph_cli.argparse.Namespace(
        thread="t8f-inject", track="track2", question="Q5", index=None,
        variant="fluid", stage0_report=None)
    assert graph_cli._fluid_stage0_path(args2) == t8f.default_stage0_report_path()


def test_default_stage0_report_path_layout():
    p = t8f.default_stage0_report_path()
    assert p.parts[-4:] == ("phase2", "task8", "stage0", t8f.STAGE0_REPORT_NAME)


# ---------------------------------------------------------------------------
# ② 出生/回收双态工厂
# ---------------------------------------------------------------------------

def test_factory_birth_mode_injects_contract(tmp_path, monkeypatch):
    s0 = _write_s0(tmp_path)
    state = _factory_env(tmp_path, monkeypatch, s0)
    factory = t8f.make_spec_factory(THREAD, "b-code", task_title="Task8 Q5")
    spec = factory(state)
    # cmd 指向出生时不存在的入口（workdir 里没有 genesis_fluid_pour.py）
    assert Path(spec.cmd[1]).name == t8f.FLUID_ENTRYPOINT
    assert not Path(spec.cmd[1]).exists()
    assert spec.cmd[0] == t8f.VENV_PY
    assert spec.coding_max_tokens == t8f.CODING_MAX_TOKENS == 32768
    assert spec.coding_timeout_s == t8f.CODING_TIMEOUT_S == 600
    assert spec.run_timeout_s == t8f.STAGE1_RUN_TIMEOUT_S == 2400
    assert spec.telemetry_relpath == t8f.RESULTS_NAME
    assert spec.billing_task == t8f.TASK == 8
    assert spec.batch_id == "b-code"
    # 攢批93：出生态固定挂 runner n=6 拆块规格（kimi-k3.1-pro 单次出生 probe E
    # 605.1s/126682 字独白截断，T8-D2 取证）；单补丁 extra 段恒空。
    assert spec.extra_prompt_sections == ()
    cs = spec.chunked_birth
    assert cs is not None
    assert cs.target_path == t8f.FLUID_ENTRYPOINT
    assert cs.chunk_count == 6
    assert len(cs.chunk_titles) == 6 and len(cs.chunk_boundaries) == 6
    titles = [t for t, _ in cs.per_call_sections]
    assert titles == ["Task8 Q5 流体拆块出生合同", "已批准 macro 提案"]
    body = cs.per_call_sections[0][1]
    assert t8f.FLUID_ENTRYPOINT in body
    assert "simpraxis.chunk.v1" in body        # sentinel 围栏交付协议
    assert '"old":""' in body.replace(" ", "")  # 拼接后单补丁出生语义
    assert "0.85" in body and "trajectory.npz" in body
    assert "outcome=sph_ok" in body  # Stage0 事实嵌入合同
    assert "Q5 厨房倒水最小闭环" in cs.per_call_sections[1][1]
    # 环境：Genesis/EGL 正确性开关 + 持久缓存 HOME（大文件只落 /mnt）
    assert spec.env_extra["GS_ENABLE_ZEROCOPY"] == "0"
    assert spec.env_extra["PYOPENGL_PLATFORM"] == "egl"
    assert spec.env_extra["SIMPRAXIS_USE_PROXY"] == "0"
    # 攢批94：coding 站增强召回（错误签名清洗 + capabilities API 金块路）
    cr = spec.coding_recall
    assert cr is not None and cr.clean_troubleshooting_hint is True
    assert cr.extra_kb == (("capabilities", t8f.FLUID_CAPABILITY_TOP_K,
                            t8f.FLUID_CAPABILITY_QUERY),)
    assert "SPH.Liquid" in t8f.FLUID_CAPABILITY_QUERY
    assert t8f.FLUID_CAPABILITY_TOP_K == 3
    # cmd 与出生合同同源（build_stage1_contract 单点）
    contract = t8f.build_stage1_contract(
        stage0_outcome="sph_ok", workdir=spec.workdir, thread_id=THREAD)
    assert list(spec.cmd) == list(contract.cmd)


def test_factory_recycle_mode_no_contract(tmp_path, monkeypatch):
    s0 = _write_s0(tmp_path)
    state = _factory_env(tmp_path, monkeypatch, s0)
    kb = tmp_path / "kb_solutions" / t8f.FLUID_SOLUTION_ID
    kb.mkdir(parents=True)
    staged = mock.Mock()
    staged.entrypoint = kb / t8f.FLUID_ENTRYPOINT
    staged.entrypoint.write_text("# recycled\n", encoding="utf-8")
    monkeypatch.setattr(t8f, "resolve_solution",
                        lambda sid: mock.Mock(stage=lambda wd: staged))
    factory = t8f.make_spec_factory(THREAD, "b-code")
    spec = factory(state)
    assert spec.extra_prompt_sections == ()
    assert spec.chunked_birth is None
    # 回收件经 stage 落进 workdir：cmd 指向 workdir 内同名入口（出生/回收
    # 两态 cmd 相同，回收态不再注入出生合同）；cmd[0] 仍是 venv 解释器
    assert Path(spec.cmd[1]).name == t8f.FLUID_ENTRYPOINT
    assert Path(spec.cmd[1]).parent == Path(spec.workdir)
    assert spec.cmd[0] == t8f.VENV_PY
    assert t8f.CODING_MAX_TOKENS == spec.coding_max_tokens  # 双态同值


def test_factory_recycle_integrity_failclosed(tmp_path, monkeypatch):
    """清单损坏（SolutionError）→ GraphStateError，绝不静默回退出生。"""
    s0 = _write_s0(tmp_path)
    state = _factory_env(tmp_path, monkeypatch, s0)
    kb = tmp_path / "kb_solutions" / t8f.FLUID_SOLUTION_ID
    kb.mkdir(parents=True)

    def _boom(sid):
        raise SolutionError("sha256 不符：genesis_fluid_pour.py")

    monkeypatch.setattr(t8f, "resolve_solution", _boom)
    factory = t8f.make_spec_factory(THREAD, "b-code")
    with pytest.raises(GraphStateError, match="stage 失败"):
        factory(state)


def test_factory_recycle_entrypoint_mismatch(tmp_path, monkeypatch):
    """回收件入口名不符 → GraphStateError（清单篡改/错件 fail-closed）。"""
    s0 = _write_s0(tmp_path)
    state = _factory_env(tmp_path, monkeypatch, s0)
    kb = tmp_path / "kb_solutions" / t8f.FLUID_SOLUTION_ID
    kb.mkdir(parents=True)
    staged = mock.Mock()
    staged.entrypoint = kb / "evil_script.py"
    staged.entrypoint.write_text("# wrong\n", encoding="utf-8")
    monkeypatch.setattr(t8f, "resolve_solution",
                        lambda sid: mock.Mock(stage=lambda wd: staged))
    factory = t8f.make_spec_factory(THREAD, "b-code")
    with pytest.raises(GraphStateError, match="入口"):
        factory(state)


def test_factory_reentry_skips_contract(tmp_path, monkeypatch):
    """同 run 重入（入口已出生）不再注入合同（old='' 补丁对已存在文件会失配）。"""
    s0 = _write_s0(tmp_path)
    state = _factory_env(tmp_path, monkeypatch, s0)
    factory = t8f.make_spec_factory(THREAD, "b-code")
    first = factory(state)
    Path(first.cmd[1]).write_text("# born\n", encoding="utf-8")
    # 钉住 run2 目录到同一 target（同 target 重入语义）
    monkeypatch.setattr(t8f, "fluid_run_dir",
                        lambda tid, idx: Path(first.workdir))
    state2 = dict(state, agent_runs=[{"status": "failed"}])
    spec2 = factory(state2)
    assert spec2.extra_prompt_sections == ()
    # 攢批93：已出生后的修补轮同样不得挂拆块规格（runner 按磁盘现状选路）。
    assert spec2.chunked_birth is None


def test_factory_d5_degraded_zero_spec(tmp_path, monkeypatch):
    s0 = _write_s0(tmp_path, _both_fail_s0())
    state = _factory_env(tmp_path, monkeypatch, s0)
    factory = t8f.make_spec_factory(THREAD, "b-code")
    with pytest.raises(GraphStateError, match="D-5"):
        factory(state)


# ---------------------------------------------------------------------------
# ③ evaluate 两级装入 + 引擎组装
# ---------------------------------------------------------------------------

_EVAL_FIELDS = {"ok", "thread_id", "wj_report_path", "schema", "stage",
                "aggregate", "judge_sources_used", "gate2_ready", "provenance"}


def test_evaluate_state_key_positive_frozen_fields(tmp_path):
    """state 键正路径：9 字段冻结、aggregate 流体键、报表不重写。"""
    wd = _write_stage1_artifacts(tmp_path)
    report = t8f.build_wj_fluid_report(
        thread_id=THREAD, solver_api="SPH.Liquid", cup=CUP,
        metrics={"entry_ratio_median": 0.9, "ratio_at_count_moment": 0.9,
                 "count_moment_frame": 12, "window_frames": 25,
                 "splash_ratio_median": 0.08,
                 "out_of_bounds_ratio_median": 0.02,
                 "target": 0.85, "meets_target": True,
                 "particle_count": N, "frame_dt_s": FRAME_DT},
        conservation={"pass": True, "violations": [], "checks": {}},
        metrics_ref={"path": t8f.RESULTS_NAME, "keys": ["solver_api"],
                     "sha256": None},
        trajectory_ref={"path": t8f.TRAJECTORY_NAME, "sha256": None},
        media=[{"kind": "video", "path": t8f.VIDEO_NAME, "sha256": None}],
        stage0_compat_ref="evidence/stage0.json", gate2_ready=True)
    wj = wd / t8f.WJ_REPORT_NAME
    wj.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    seam = t8f.make_evaluate_seam(THREAD)
    result = seam({t8f.WJ_REPORT_STATE_KEY: str(wj)})
    assert set(result) == _EVAL_FIELDS
    assert result["ok"] and result["wj_report_path"] == str(wj)
    assert result["aggregate"]["cup_entry_rate"] == 0.9
    assert result["aggregate"]["meets_target"] is True
    assert result["aggregate"]["conservation_pass"] is True
    assert result["judge_sources_used"] == [t8f.JUDGE_GEOMETRY]


def test_evaluate_assembles_from_results(tmp_path):
    """主生产路径：results.json+trajectory.npz → 引擎现算组装、0600、可重放。"""
    wd = _write_stage1_artifacts(tmp_path)
    s0 = str(tmp_path / "stage0" / t8f.STAGE0_REPORT_NAME)
    seam = t8f.make_evaluate_seam(THREAD)
    state = {"agent_runs": [{"telemetry_uri": str(wd / t8f.RESULTS_NAME)}],
             t8f.STAGE0_REPORT_STATE_KEY: s0}
    result = seam(state)
    assert set(result) == _EVAL_FIELDS
    wj_path = Path(result["wj_report_path"])
    assert wj_path == wd / t8f.WJ_REPORT_NAME
    assert wj_path.is_file()
    assert stat.S_IMODE(wj_path.stat().st_mode) == 0o600
    # 引擎现算：入杯中位 0.90、目标 0.85、达成、守恒通过、飞溅 0.08、越界 0.02
    agg = result["aggregate"]
    assert agg["cup_entry_rate"] == pytest.approx(0.90)
    assert agg["target"] == 0.85
    assert agg["meets_target"] is True
    assert agg["conservation_pass"] is True
    assert agg["splash_rate"] == pytest.approx(0.08)
    # out_of_bounds = 杯底下 1 + 墙外 1 = 2 → 0.02 ≤ 0.02 限 → 守恒通过
    assert agg["out_of_bounds_rate"] == pytest.approx(0.02)
    assert result["gate2_ready"] is True
    doc = json.loads(wj_path.read_text(encoding="utf-8"))
    assert doc["stage0_compat_ref"] == s0
    assert doc["metrics"]["window_frames"] == 25
    assert doc["metrics"]["count_moment_frame"] == 12
    assert doc["conservation"]["pass"] is True
    # 数字可回指到字节（两件 sha256 非空 64hex）
    assert len(doc["metrics_ref"]["sha256"]) == 64
    assert len(doc["trajectory_ref"]["sha256"]) == 64
    # 引擎生成证据件：窗内逐帧入杯比例曲线
    csv = (wd / t8f.CURVE_CSV_NAME).read_text(encoding="utf-8").strip().splitlines()
    assert csv[0] == "frame,in_cup_ratio"
    assert len(csv) == 1 + 25
    assert csv[1] == "5,0.900000"  # offset=30-25
    # 节点可重放：wj_report 已在 → 一级命中，不重写
    before = wj_path.stat().st_mtime_ns
    result2 = seam(state)
    assert result2["wj_report_path"] == str(wj_path)
    assert wj_path.stat().st_mtime_ns == before


def test_evaluate_conservation_violation_is_result(tmp_path):
    """越界违例（0.05>0.02）：组装成功、守恒 pass=False 如实上报（不是异常）。"""
    wd = _write_stage1_artifacts(tmp_path, oob_outside=5)
    seam = t8f.make_evaluate_seam(THREAD)
    result = seam({"agent_runs": [{"telemetry_uri": str(wd / t8f.RESULTS_NAME)}],
                   t8f.STAGE0_REPORT_STATE_KEY: "evidence/stage0.json"})
    assert result["ok"] is True
    assert result["aggregate"]["conservation_pass"] is False
    # out_of_bounds = 杯底下 1 + 墙外 5 = 6 → 0.06 > 0.02 限
    assert result["aggregate"]["out_of_bounds_rate"] == pytest.approx(0.06)
    # 入杯率引擎现算（86/100=0.86）；0.86>=0.85 仍达成目标，守恒违例如实并存
    assert result["aggregate"]["cup_entry_rate"] == pytest.approx(0.86)
    assert result["aggregate"]["meets_target"] is True


def test_evaluate_assembly_failclosed_variants(tmp_path):
    seam = t8f.make_evaluate_seam(THREAD)
    s0 = "evidence/stage0.json"

    def _state(wd):
        return {"agent_runs": [{"telemetry_uri": str(wd / t8f.RESULTS_NAME)}],
                t8f.STAGE0_REPORT_STATE_KEY: s0}

    # 缺 results.json
    wd = tmp_path / "a"
    wd.mkdir()
    (wd / t8f.VIDEO_NAME).write_bytes(b"\x00")
    with pytest.raises(GraphStateError, match="results.json"):
        seam(_state(wd))
    # 衍生自报字段禁（in_cup_ratio）
    wd = _write_stage1_artifacts(
        tmp_path / "b", results_override={"in_cup_ratio": 0.5})
    with pytest.raises(GraphStateError, match="衍生字段"):
        seam(_state(wd))
    # 粒子超硬天花板
    wd = _write_stage1_artifacts(
        tmp_path / "c", results_override={"n_particles": 20001})
    with pytest.raises(GraphStateError, match="20000"):
        seam(_state(wd))
    # 杯几何非法（rim<=bottom）
    wd = _write_stage1_artifacts(
        tmp_path / "d",
        results_override={"cup": {"inner_radius": 0.1, "bottom_z": 0.2,
                                  "rim_z": 0.1}})
    with pytest.raises(GraphStateError, match="cup"):
        seam(_state(wd))
    # 缺轨迹（入杯语义必须从几何现算，不补不猜）
    wd = _write_stage1_artifacts(tmp_path / "e")
    (wd / t8f.TRAJECTORY_NAME).unlink()
    with pytest.raises(GraphStateError, match="trajectory"):
        seam(_state(wd))
    # N 与 n_particles 失配
    wd = _write_stage1_artifacts(
        tmp_path / "f", results_override={"n_particles": 99})
    with pytest.raises(GraphStateError, match="不一致"):
        seam(_state(wd))
    # NaN 坐标
    wd = _write_stage1_artifacts(tmp_path / "g")
    pos = np.load(wd / t8f.TRAJECTORY_NAME)["positions"].copy()
    pos[0, 0, 0] = np.nan
    np.savez_compressed(wd / t8f.TRAJECTORY_NAME, positions=pos)
    with pytest.raises(GraphStateError, match="非有限"):
        seam(_state(wd))
    # 轨迹放不下沉降窗（85% 语义算不出即 STOP）
    wd = _write_stage1_artifacts(tmp_path / "h")
    np.savez_compressed(wd / t8f.TRAJECTORY_NAME,
                        positions=_trajectory()[:10])
    (wd / t8f.MANIFEST_NAME).write_text(
        json.dumps({"n_frames": 10, "frame_dt_s": FRAME_DT}), encoding="utf-8")
    with pytest.raises(GraphStateError, match="沉降窗"):
        seam(_state(wd))
    # manifest.n_frames 与轨迹失配（视频/轨迹必须同一次运行产出）
    wd = _write_stage1_artifacts(
        tmp_path / "i", manifest_override={"n_frames": 999})
    with pytest.raises(GraphStateError, match="n_frames"):
        seam(_state(wd))
    # 缺 stage0 溯源键
    wd = _write_stage1_artifacts(tmp_path / "j")
    with pytest.raises(GraphStateError, match="stage0_report_path"):
        seam({"agent_runs": [
            {"telemetry_uri": str(wd / t8f.RESULTS_NAME)}]})
    # 双缺：无 state 键且无 telemetry
    with pytest.raises(GraphStateError, match="无法定位"):
        seam({})
    # video 缺失：Gate2 真人裁决媒体必写
    wd = _write_stage1_artifacts(tmp_path / "k", video=False)
    with pytest.raises(GraphStateError, match="video"):
        seam(_state(wd))


def test_evaluate_state_key_missing_file_loud(tmp_path):
    """state 键一旦给出：缺件响亮报错，不静默降级改道。"""
    seam = t8f.make_evaluate_seam(THREAD)
    with pytest.raises(GraphStateError, match="不存在"):
        seam({t8f.WJ_REPORT_STATE_KEY: str(tmp_path / "nope.json")})


# ---------------------------------------------------------------------------
# ④ Gate1 接缝 + client 方法
# ---------------------------------------------------------------------------

class _FakeFeishu:
    def __init__(self):
        self.calls: list[dict] = []

    def request_gate1_fluid_approval(self, thread_id, task_title, proposals,
                                     *, stage0_facts, max_wait=None):
        self.calls.append({"thread_id": thread_id, "task_title": task_title,
                           "proposals": proposals, "stage0_facts": stage0_facts})
        return {"decision": "approve", "choice": 0, "human_notes": "批"}


def test_gate1_seam_sph_ok_sends_fluid_card(tmp_path):
    s0 = _write_s0(tmp_path)
    fake = _FakeFeishu()
    seam = t8f.make_gate1_seam(fake, THREAD, "Task8 Q5 Stage1")
    out = seam(_state(s0))
    assert out["decision"] == "approve"
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["stage0_facts"]["outcome"] == "sph_ok"
    assert call["stage0_facts"]["propose_run"] is True
    assert call["stage0_facts"]["solver_api"] == "Weird.Liquid"
    assert call["stage0_facts"]["platform"] == t8f.PLATFORM
    assert call["proposals"][0]["name"] == "Q5 厨房倒水最小闭环"


def test_gate1_seam_d5_zero_card(tmp_path):
    s0 = _write_s0(tmp_path, _both_fail_s0())
    fake = _FakeFeishu()
    seam = t8f.make_gate1_seam(fake, THREAD, "Task8 Q5 Stage1")
    with pytest.raises(GraphStateError, match="D-5"):
        seam(_state(s0))
    assert fake.calls == []  # 零卡片：绝不把降级伪装成运行案


def test_client_gate1_fluid_send_and_rehang(tmp_path):
    from orchestrator.feishu_gateway.client import FeishuGateway
    gw = FeishuGateway("cli_x", "secret_x", "ou_owner",
                       db_path=tmp_path / "state" / "pending.db",
                       evidence_dir=tmp_path / "ev", run_dir=str(tmp_path),
                       start_ws=False)
    sent: list[dict] = []

    def _fake_send(open_id, card_v1, card_v2, *, form_required=True):
        sent.append({"v1": card_v1, "v2": card_v2})
        return {"message_id": "om_msg_fluid_1", "chat_id": "oc_chat_1",
                "schema": "2.0"}

    gw._send_interactive = _fake_send
    gw._await_decision = lambda tid, gate, max_wait=None: {
        "decision": "approve", "choice": 0, "human_notes": "真人批"}
    facts = {"outcome": "sph_ok", "stage0_schema": "simpraxis.task8.stage0_fluid.v1",
             "solver_api": "Weird.Liquid", "n_particles": 1000,
             "gpu": {"name": "A800", "peak_mib": 1228.8},
             "versions": {"genesis": "1.3.3"}, "ts_utc": "2026-10-26T00:00:00Z",
             "particle_cap": 20000, "cup_entry_target": 0.85,
             "settling_window_s": 0.5, "propose_run": True}
    props = [{"name": "p1"}]
    out = gw.request_gate1_fluid_approval(
        "t8f-client", "Task8 Q5", props, stage0_facts=facts)
    assert out["decision"] == "approve"
    assert len(sent) == 1
    # 2.0 主卡带 Stage0 流体能力插页与专属标题
    body = sent[0]["v2"]["body"]["elements"]
    assert "Stage0 流体能力事实" in body[1]["text"]["content"]
    assert "厨房倒水" in sent[0]["v2"]["header"]["title"]["content"]
    assert "Stage0 流体能力事实" in sent[0]["v1"]["elements"][1]["content"]
    row = gw.store.get_pending("t8f-client", 1)
    assert row["card_message_id"] == "om_msg_fluid_1"
    # 重挂：PENDING 行 + 已发卡 → 不重发
    gw._send_interactive = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("重挂不得重发卡片"))
    gw._await_decision = lambda tid, gate, max_wait=None: {
        "decision": "approve", "choice": 0, "human_notes": "重挂后批"}
    out2 = gw.request_gate1_fluid_approval(
        "t8f-client", "Task8 Q5", props, stage0_facts=facts)
    assert out2["decision"] == "approve"


# ---------------------------------------------------------------------------
# ⑤ 卡片结构 + Gate2 三问
# ---------------------------------------------------------------------------

def _facts() -> dict:
    return {"outcome": "sph_ok",
            "stage0_schema": "simpraxis.task8.stage0_fluid.v1",
            "solver_api": "Weird.Liquid", "n_particles": 1000,
            "gpu": {"name": "Fake A800", "peak_mib": 1228.8},
            "versions": {"genesis": "1.3.3", "torch": "2.4.1"},
            "ts_utc": "2026-10-26T00:00:00Z",
            "particle_cap": 20000, "cup_entry_target": 0.85,
            "settling_window_s": 0.5}


def test_gate1_card_fluid_structure():
    from orchestrator.feishu_gateway import cards
    props = [{"name": "p1", "sim_engine": "Genesis", "first_principles": "x",
              "artifacts": ["video.mp4"]}]
    c2 = cards.build_gate1_card_fluid("T8", props, _facts())
    assert c2["schema"] == "2.0"
    assert "Stage0 已证流体能力" in c2["header"]["title"]["content"]
    el = c2["body"]["elements"]
    assert "Stage0 流体能力事实" in el[1]["text"]["content"]
    # md_escape 会把 sph_ok 的下划线转义为 sph\_ok
    assert "outcome：**sph\\_ok**" in el[1]["text"]["content"]
    assert "Weird.Liquid" in el[1]["text"]["content"]
    assert "0.85" in el[1]["text"]["content"]
    assert "方案 1" in el[2]["text"]["content"]  # 方案 div 紧随其后
    c1 = cards.build_gate1_card_fluid_v1("T8", props, _facts())
    assert "Stage0 流体能力事实" in c1["elements"][1]["content"]
    assert c1["header"]["title"]["content"] == c2["header"]["title"]["content"]
    # 表单仍可交互（批准/打回按钮 value 结构不破）
    btns = [x for x in el if x.get("tag") == "form"][0]["elements"]
    assert btns[1]["value"] == {"gate": 1, "decision": "approve", "choice": 0}


def test_gate2_metrics_fluid_branch_and_legacy():
    from orchestrator.feishu_gateway import cards
    md = cards._gate2_metrics_md("T", {
        "wj_report_path": "/runs/wj_report.json",
        "aggregate": {"cup_entry_rate": 0.9, "target": 0.85,
                      "meets_target": True, "conservation_pass": True,
                      "splash_rate": 0.08, "out_of_bounds_rate": 0.02},
        "stage": "stage1_pour", "judge_sources_used": ["几何机判"],
        "gate2_ready": True,
    }, None)
    assert "Task8 流体" in md and "入杯率中位数：0.900" in md
    assert "目标 0.85" in md and "达成" in md
    assert "stage1_pour" in md and "几何机判" in md
    # Task7 WJ-Lite 分支不破（aggregate 无 cup_entry_rate 键）
    md2 = cards._gate2_metrics_md("T", {
        "wj_report_path": "/runs/wj_report.json",
        "aggregate": {"success": 1, "episodes": 2, "success_rate": 0.5},
        "stage": "stage1_spatial", "judge_sources_used": ["官方"],
        "gate2_ready": True,
    }, None)
    assert "WJ-Lite" in md2 and "1/2 局成功" in md2
    # 旧式扁平 metrics 兜底不破
    md3 = cards._gate2_metrics_md("T", {"pose_error": 0.1}, None)
    assert "位姿误差" in md3
    # rungs 嵌套路径不破（Phase1 task7）
    md4 = cards._gate2_metrics_md("T", {
        "rungs": {"rung1": {"pass": True, "checks": {}},
                  "rung2": {"pass": True, "checks": {}},
                  "rung3": {"pass": True, "checks": {}},
                  "rung4": None}}, None)
    assert "Rung" in md4


def test_gate2_explanation_fluid(tmp_path):
    wd = _write_stage1_artifacts(tmp_path)
    report = t8f.build_wj_fluid_report(
        thread_id=THREAD, solver_api="SPH.Liquid", cup=CUP,
        metrics={"entry_ratio_median": 0.9, "ratio_at_count_moment": 0.9,
                 "count_moment_frame": 12, "window_frames": 25,
                 "splash_ratio_median": 0.08,
                 "out_of_bounds_ratio_median": 0.02,
                 "target": 0.85, "meets_target": True,
                 "particle_count": N, "frame_dt_s": FRAME_DT},
        conservation={"pass": True, "violations": [], "checks": {}},
        metrics_ref={"path": t8f.RESULTS_NAME, "keys": ["solver_api"],
                     "sha256": None},
        trajectory_ref={"path": t8f.TRAJECTORY_NAME, "sha256": None},
        media=[{"kind": "video", "path": t8f.VIDEO_NAME, "sha256": None}],
        stage0_compat_ref="evidence/stage0.json", gate2_ready=True)
    wj = wd / t8f.WJ_REPORT_NAME
    wj.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    state = {"chosen": {"name": "Q5 厨房倒水最小闭环"},
             "metrics": {"wj_report_path": str(wj)},
             "telemetry_uri": str(wd / t8f.RESULTS_NAME)}
    expl = t8f.build_gate2_explanation(state, {"duration_ms": 12000})
    assert set(expl) == {"task", "problem", "video"}
    assert "Q5 厨房倒水最小闭环" in expl["task"]
    assert "85%" in expl["problem"]
    assert "入杯率中位数 0.900" in expl["problem"]
    assert "守恒机判通过" in expl["problem"]
    assert "30 帧" in expl["video"] and "12.0 s" in expl["video"]
    # 缺报表：退回纯叙事不编数字（键齐但无聚合行）
    expl2 = t8f.build_gate2_explanation({"chosen": {}}, {})
    assert set(expl2) == {"task", "problem", "video"}
    assert "本次聚合" not in expl2["problem"]


# ---------------------------------------------------------------------------
# ⑥ prompts 模块纯函数
# ---------------------------------------------------------------------------

def test_prompts_birth_contract_and_facts():
    from orchestrator.graph import task8_fluid_prompts as prompts
    body = prompts.stage_birth_contract(
        script_name="genesis_fluid_pour.py",
        cmd_line="/mnt/venvs/simpraxis/bin/python genesis_fluid_pour.py",
        stage0_facts_text="outcome=sph_ok",
        particle_cap=20000, vram_abort_gb=35.0, max_reductions=2,
        settling_window_s=0.5, cup_entry_target=0.85)
    assert "genesis_fluid_pour.py" in body
    assert '"old":""' in body.replace(" ", "")  # 单次 JSON 补丁出生语义
    assert "trajectory.npz" in body and "results.json" in body
    assert prompts.RESULTS_SCHEMA in body
    assert "0.85" in body and "20000" in body
    assert "outcome=sph_ok" in body
    facts = prompts.stage0_facts_text({
        "outcome": "sph_ok", "stage0_schema": "simpraxis.task8.stage0_fluid.v1",
        "solver_api": "Weird.Liquid", "n_particles": 1000,
        "gpu": {"name": "A800", "peak_mib": 1228.8},
        "versions": {"genesis": "1.3.3"}, "ts_utc": "2026-10-26T00:00:00Z",
        "particle_cap": 20000, "cup_entry_target": 0.85,
        "settling_window_s": 0.5})
    assert "outcome=sph_ok" in facts and "Weird.Liquid" in facts
    assert "A800" in facts and "genesis=1.3.3" in facts


def test_prompts_chunk_contract_keeps_machine_requirements():
    """攢批93：拆块版合同与单次版共享机检要求正文（防两版分叉）。"""
    from orchestrator.graph import task8_fluid_prompts as prompts
    kwargs = dict(
        script_name="genesis_fluid_pour.py",
        cmd_line="/mnt/venvs/simpraxis/bin/python genesis_fluid_pour.py",
        stage0_facts_text="outcome=sph_ok",
        particle_cap=20000, vram_abort_gb=35.0, max_reductions=2,
        settling_window_s=0.5, cup_entry_target=0.85)
    single = prompts.stage_birth_contract(**kwargs)
    chunk = prompts.chunk_birth_contract(n_chunks=6, **kwargs)
    # 导语不同（交付协议），导语之后的机检要求逐字同一份。
    assert single.split("\n\n", 1)[1] == chunk.split("\n\n", 1)[1]
    # 拆块版导语含围栏协议与 n=6、old 空串补丁语义。
    head = chunk.split("\n\n", 1)[0]
    assert "simpraxis.chunk.v1" in head and "6 个顺序调用" in head
    assert '"old":""' in head.replace(" ", "")


def test_fluid_chunk_titles_and_boundaries_frozen_shape():
    """n=6 工厂规格：六标题/六边界等长非空、下标 fail-loud、边界互不重复。"""
    from orchestrator.graph import task8_fluid_chunks as fc
    assert fc.FLUID_CHUNK_COUNT == 6
    assert len(fc.FLUID_CHUNK_TITLES) == 6
    assert all(t.strip() for t in fc.FLUID_CHUNK_TITLES)
    boundaries = tuple(fc.build_chunk_boundary(i) for i in range(6))
    assert len(boundaries) == 6 and all("行数预算" in b for b in boundaries)
    assert len(set(boundaries)) == 6
    for bad in (-1, 6, True, False, "0"):
        with pytest.raises(ValueError):
            fc.build_chunk_boundary(bad)


# ---------------------------------------------------------------------------
# ⑦ WJ 流体报告校验器（结构违规 fail-closed）
# ---------------------------------------------------------------------------

def test_validate_wj_fluid_report_rejects_structural_bugs():
    good = t8f.build_wj_fluid_report(
        thread_id=THREAD, solver_api="SPH.Liquid", cup=CUP,
        metrics={"entry_ratio_median": 0.9, "ratio_at_count_moment": 0.9,
                 "count_moment_frame": 12, "window_frames": 25,
                 "splash_ratio_median": 0.08,
                 "out_of_bounds_ratio_median": 0.02,
                 "target": 0.85, "meets_target": True,
                 "particle_count": N, "frame_dt_s": FRAME_DT},
        conservation={"pass": True, "violations": [], "checks": {}},
        metrics_ref={"path": t8f.RESULTS_NAME, "keys": ["solver_api"],
                     "sha256": None},
        trajectory_ref={"path": t8f.TRAJECTORY_NAME, "sha256": None},
        media=[{"kind": "video", "path": t8f.VIDEO_NAME, "sha256": None}],
        stage0_compat_ref="evidence/stage0.json", gate2_ready=True)
    assert t8f.validate_wj_fluid_report(good) == []

    def _bad(**over):
        import copy
        doc = copy.deepcopy(good)
        for k, v in over.items():
            if v is ...:
                doc.pop(k)
            else:
                doc[k] = v
        return doc

    # meets_target 与 entry/target 矛盾
    m = dict(good["metrics"], meets_target=False)
    errs = t8f.validate_wj_fluid_report(_bad(metrics=m))
    assert any("meets_target" in e for e in errs)
    # 判分来源冒充
    assert t8f.validate_wj_fluid_report(_bad(judge_source="模型判"))
    # 顶层 media 缺失
    assert t8f.validate_wj_fluid_report(_bad(media=[]))
    # media 重复路径
    media = good["media"] + [{"kind": "log", "path": t8f.VIDEO_NAME,
                              "sha256": None}]
    assert t8f.validate_wj_fluid_report(_bad(media=media))
    # gate2_ready=true 但无 video
    media_nov = [{"kind": "log", "path": t8f.RESULTS_NAME, "sha256": None}]
    assert t8f.validate_wj_fluid_report(_bad(media=media_nov))
    # trajectory_ref.path 不是 trajectory.npz
    tref = {"path": "video.mp4", "sha256": None}
    assert t8f.validate_wj_fluid_report(_bad(trajectory_ref=tref))
    # ts 非严格 Z
    assert t8f.validate_wj_fluid_report(_bad(ts_utc="2026-10-26 00:00:00"))
    # schema 位错误
    assert t8f.validate_wj_fluid_report(_bad(schema="simpraxis.wj_report.v1"))
    # 缺必填字段
    assert t8f.validate_wj_fluid_report(_bad(solver_api=...))
    # 非对象
    assert t8f.validate_wj_fluid_report([1, 2])
    # build 后自校验：坏 ts 直接 WjFluidReportError（不让非法件出引擎）
    with pytest.raises(t8f.WjFluidReportError):
        t8f.build_wj_fluid_report(
            thread_id=THREAD, solver_api="SPH.Liquid", cup=CUP,
            metrics=good["metrics"], conservation=good["conservation"],
            metrics_ref=good["metrics_ref"],
            trajectory_ref=good["trajectory_ref"], media=good["media"],
            stage0_compat_ref="evidence/stage0.json", gate2_ready=True,
            ts_utc="not-a-timestamp")
