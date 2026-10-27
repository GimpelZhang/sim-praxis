"""Phase 2 Task7 B 线 CLI/工厂/评估/Gate1/Gate2 接线测（攢批第 71 条；0 付费）。

覆盖（全部零网络/零容器/零 tour 执行，硬规则 10；fake 产物只落 tmp_path）：
  1. CLI 组合守卫：--variant 仅 task=7；--stage0-report 仅 libero（exit 2）；
  2. 出生/回收双态工厂：内容层无 track1_tools/03_libero → 出生合同注入 +
     cmd 指向出生时不存在的入口；有 → resolve+stage（清单损坏 fail-closed
     不回退出生）；同 run 重入不重复注入合同；D-4 零 RunSpec；
  3. evaluate 两级装入：state 键 → telemetry 同目录 → results.json 引擎组装
     （聚合引擎现算、衍生自报字段/坏 judge/局数失配 fail-closed、9 字段冻结）；
  4. Gate1 接缝：compatible → 真人卡方法（Stage0 事实随卡）；D-4 → 零卡片
     GraphStateError；client 重挂不重发；
  5. 卡片结构：2.0/1.0 Stage0 插页 + Gate2 WJ-Lite 分支不破旧路径。
"""
from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path
from unittest import mock

import pytest

from orchestrator.graph import cli as graph_cli
from orchestrator.graph import task7_libero as t7
from orchestrator.graph.nodes import GraphStateError
from orchestrator.solutions import SolutionError

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE_PATH = REPO_ROOT / "sandbox" / "probes" / "probe_libero_compat.py"

_spec = importlib.util.spec_from_file_location("probe_libero_compat_wiring",
                                               PROBE_PATH)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

# 与 test_task7_libero_seam 同构的合成 Stage0 件构造器（hermetic）。
def _ok_import_chain() -> list[dict]:
    versions = [("numpy", "1.26.4"), ("torch", "2.4.1"), ("mujoco", "3.15.0"),
                ("robosuite", "1.4.0"), ("bddl", "1.0.1"),
                ("libero", "libero-import-ok")]
    return [probe.summarize_import(m, 0, f"{v}\n", "") for m, v in versions]


def _ok_install_steps() -> list[dict]:
    constraints = "/tmp/constraints.txt"
    py = probe.venv_python(probe.VENV_PATH)
    return [
        {"target": "venv_create",
         "argv": probe.venv_create_cmd(probe.VENV_PATH), "rc": 0,
         "log_path": "/tmp/venv_create.log", "tail_verbatim": ".pth 引导完成"},
        {"target": "robosuite_bddl",
         "argv": probe.pip_install_chain_cmd(probe.VENV_PATH, constraints),
         "rc": 0,
         "log_path": "/tmp/pip_robosuite_bddl.log",
         "tail_verbatim": "Successfully installed robosuite-1.4.0 bddl-1.0.1"},
        {"target": "isolated_leaf_deps",
         "argv": probe.pip_install_leaf_deps_cmd(probe.VENV_PATH, constraints),
         "rc": 0,
         "log_path": "/tmp/pip_isolated_leaf_deps.log",
         "tail_verbatim": "Successfully installed termcolor future"},
        {"target": "libero_src_copy",
         "argv": probe.copy_tour_src_cmd(
             probe.VENV_PATH, probe.TOUR_DIR, probe.WRITABLE_TOUR_COPY),
         "rc": 0,
         "log_path": "/tmp/libero_src_copy.log",
         "tail_verbatim": "src=/mnt/assets\ndst=/home/agent/runs\ncopied ok"},
        {"target": "libero_pkg_init_shim",
         "argv": probe.libero_pkg_init_shim_cmd(py, probe.WRITABLE_TOUR_COPY),
         "rc": 0,
         "log_path": "/tmp/libero_pkg_init_shim.log",
         "tail_verbatim": "shim created: empty libero/__init__.py marker"},
        {"target": "libero_editable",
         "argv": probe.pip_install_libero_cmd(
             probe.VENV_PATH, probe.WRITABLE_TOUR_COPY),
         "rc": 0,
         "log_path": "/tmp/pip_libero_editable.log",
         "tail_verbatim": "Successfully installed libero"},
    ]


def _ok_report(**overrides) -> dict:
    report = {
        "schema": probe.REPORT_SCHEMA,
        "version": probe.REPORT_VERSION,
        "ok": True,
        "outcome": "compatible",
        "venv_path": probe.VENV_PATH,
        "index_url": probe.INDEX_URL,
        "tour_dir": probe.TOUR_DIR,
        "installed": {"numpy": "1.26.4", "torch": "2.4.1", "mujoco": "3.15.0",
                      "mujoco-py": None, "robosuite": "1.4.0",
                      "bddl": "1.0.1", "libero": "0.1.0"},
        "install_steps": _ok_install_steps(),
        "import_chain": _ok_import_chain(),
        "shared_pins_observed": {"numpy": "1.26.4", "torch": "2.4.1",
                                 "mujoco": "3.15.0"},
        "pin_facts": {"numpy_is_shared_pin": True, "torch_is_shared_pin": True,
                      "mujoco_is_shared_pin": True, "mujoco_diverged": False},
        "reasons": [],
        "notes": [],
        "ts_utc": "2026-10-24T03:00:00Z",
    }
    report.update(overrides)
    return report


def _write_stage0(tmp_path: Path, **overrides) -> Path:
    p = tmp_path / "stage0" / "stage0_compat_report.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(_ok_report(**overrides), ensure_ascii=False),
                 encoding="utf-8")
    return p


def _degraded_report() -> dict:
    """真实降级件：装步 rc≠0 + import 链断，outcome 走 decide_outcome 单点
    推导（探针禁止手标结局——overrides 硬改 outcome 会被 validate 拒）。"""
    steps = _ok_install_steps()
    steps[-1] = dict(steps[-1], rc=1,
                     tail_verbatim="ERROR: No matching distribution libero")
    chain = _ok_import_chain()
    chain[-1] = probe.summarize_import(
        "libero", 1, "", "ImportError: No module named 'libero'")
    outcome, pin_facts, reasons, notes = probe.decide_outcome(steps, chain)
    assert outcome == "conflict_isolated_failed"  # 合成前提自检
    report = _ok_report(
        ok=False, outcome=outcome, install_steps=steps, import_chain=chain,
        pin_facts=pin_facts, reasons=reasons, notes=notes,
        degradation=probe.DEGRADATION)
    assert probe.validate_report(report) == []  # 合成件先过校验器
    return report


def _write_stage0_degraded(tmp_path: Path) -> Path:
    p = tmp_path / "stage0" / "stage0_compat_report.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(_degraded_report(), ensure_ascii=False),
                 encoding="utf-8")
    return p


def _state(s0_path: Path, **extra) -> dict:
    state = {
        "thread_id": "t7l-wiring",
        "track": "track1",
        "proposals": [{"name": "libero_spatial 最小闭环",
                       "sim_engine": "LIBERO/robosuite",
                       "first_principles": "官方 check_success",
                       "artifacts": ["video.mp4"]}],
        "chosen": {"name": "libero_spatial 最小闭环",
                   t7.STAGE0_REPORT_STATE_KEY: str(s0_path)},
        "events": [],
    }
    state.update(extra)
    return state


def _factory_env(tmp_path: Path, monkeypatch, s0_path: Path):
    """工厂测试公共环境：RUN_DIR + 空内容层 + 兼容 Stage0。"""
    monkeypatch.setenv("SIMPRAXIS_RUN_DIR", str(tmp_path / "runs"))
    empty_kb = tmp_path / "kb_solutions"
    empty_kb.mkdir()
    monkeypatch.setattr(t7, "solutions_root", lambda: empty_kb)
    return _state(s0_path)


def _valid_results_doc(**task_overrides) -> dict:
    task = {"task_id": "libero_spatial_basketball_in_hoop",
            "bddl": "libero_spatial/basketball_in_hoop.bddl",
            "success": 1, "episodes": 2}
    task.update(task_overrides)
    return {
        "schema": "simpraxis.task7.libero_results.v1",
        "stage": t7.STAGE1_SPATIAL,
        "suite_name": "libero_spatial",
        "n_episodes_per_task": 2,
        "judge": "libero_official",
        "tasks": [task],
    }


# ---------------------------------------------------------------------------
# ① CLI 组合守卫
# ---------------------------------------------------------------------------

def test_cli_variant_validation_exit_codes():
    # --variant 非 task7 → exit 2（不进 _invoke）
    with mock.patch.object(graph_cli, "_invoke", return_value=0) as inv:
        rc = graph_cli.main(["run", "--thread", "t", "--track", "track1",
                             "--index", "4", "--batch-id", "b1", "--task", "8",
                             "--variant", "libero"])
        assert rc == 2
        inv.assert_not_called()
        # --stage0-report 无 variant → exit 2
        rc = graph_cli.main(["run", "--thread", "t", "--track", "track1",
                             "--index", "4", "--batch-id", "b1", "--task", "7",
                             "--stage0-report", "/x"])
        assert rc == 2
        inv.assert_not_called()
        # 合法组合进 _invoke（task=7 + variant）
        rc = graph_cli.main(["run", "--thread", "t7l-ok", "--track", "track1",
                             "--index", "4", "--batch-id", "b1", "--task", "7",
                             "--variant", "libero",
                             "--coding-batch-id", "b2"])
        assert rc == 0
        inv.assert_called_once()


def test_cli_initial_state_injects_stage0(tmp_path):
    s0 = _write_stage0(tmp_path)
    args = graph_cli.argparse.Namespace(
        thread="t7l-inject", track="track1", question=None, index=4,
        variant="libero", stage0_report=str(s0))
    state = graph_cli._initial_state(args, {"name": "LIBERO 巡礼"})
    assert state[t7.STAGE0_REPORT_STATE_KEY] == str(s0)
    # 缺省路径解析（不传 --stage0-report）
    args2 = graph_cli.argparse.Namespace(
        thread="t7l-inject", track="track1", question=None, index=4,
        variant="libero", stage0_report=None)
    assert graph_cli._libero_stage0_path(args2) == t7.default_stage0_report_path()


def test_default_stage0_report_path_layout():
    p = t7.default_stage0_report_path()
    assert p.parts[-4:] == ("phase2", "task7", "stage0", t7.STAGE0_REPORT_NAME)


# ---------------------------------------------------------------------------
# ② 出生/回收双态工厂
# ---------------------------------------------------------------------------

def test_factory_birth_mode_injects_contract(tmp_path, monkeypatch):
    s0 = _write_stage0(tmp_path)
    state = _factory_env(tmp_path, monkeypatch, s0)
    factory = t7.make_spec_factory("t7l-wiring", "b-code",
                                   stage=t7.STAGE1_SPATIAL)
    spec = factory(state)
    # cmd 指向出生时不存在的入口（workdir 里没有 stage1_minimal.py）
    assert Path(spec.cmd[1]).name == "stage1_minimal.py"
    assert not Path(spec.cmd[1]).exists()
    assert spec.cmd[0] == t7.VENV_PY
    assert spec.coding_max_tokens == 32768
    assert spec.coding_timeout_s == 600
    assert spec.run_timeout_s == 1200
    # 出生合同 + 已批准 macro 提案两段注入
    titles = [t for t, _ in spec.extra_prompt_sections]
    assert titles == ["Task7 LIBERO stage1_spatial 出生合同",
                      "已批准 macro 提案"]
    body = spec.extra_prompt_sections[0][1]
    assert "stage1_minimal.py" in body
    assert "results.json" in body and "video.mp4" in body
    assert "outcome=compatible" in body  # Stage0 事实嵌入合同
    assert "libero_spatial 最小闭环" in spec.extra_prompt_sections[1][1]
    # .libero 配置预置（防 input() EOFError）
    cfg = Path(spec.workdir) / ".libero" / "config.yaml"
    assert cfg.is_file() and cfg.stat().st_size > 0
    assert spec.env_extra["MUJOCO_GL"] == "egl"
    assert spec.env_extra["LIBERO_CONFIG_PATH"] == str(cfg.parent)


def test_factory_recycle_mode_no_contract(tmp_path, monkeypatch):
    s0 = _write_stage0(tmp_path)
    state = _factory_env(tmp_path, monkeypatch, s0)
    # 内容层已回收：方案目录存在 + resolve_solution 返回合法 staged
    kb = tmp_path / "kb_solutions" / t7.LIBERO_SOLUTION_ID
    kb.mkdir(parents=True)
    staged = mock.Mock()
    staged.entrypoint = kb / "stage1_minimal.py"
    staged.entrypoint.write_text("# recycled\n", encoding="utf-8")
    monkeypatch.setattr(t7, "resolve_solution",
                        lambda sid: mock.Mock(stage=lambda wd: staged))
    factory = t7.make_spec_factory("t7l-wiring", "b-code")
    spec = factory(state)
    assert spec.extra_prompt_sections == ()
    assert spec.cmd[1] == str(staged.entrypoint)
    assert t7.CODING_MAX_TOKENS == spec.coding_max_tokens  # 双态同值


def test_factory_recycle_integrity_failclosed(tmp_path, monkeypatch):
    """清单损坏（SolutionError）→ GraphStateError，绝不静默回退出生。"""
    s0 = _write_stage0(tmp_path)
    state = _factory_env(tmp_path, monkeypatch, s0)
    kb = tmp_path / "kb_solutions" / t7.LIBERO_SOLUTION_ID
    kb.mkdir(parents=True)

    def _boom(sid):
        raise SolutionError("sha256 不符：stage1_minimal.py")

    monkeypatch.setattr(t7, "resolve_solution", _boom)
    factory = t7.make_spec_factory("t7l-wiring", "b-code")
    with pytest.raises(GraphStateError, match="stage 失败"):
        factory(state)


def test_factory_reentry_skips_contract(tmp_path, monkeypatch):
    """同 run 重入（入口已出生）不再注入合同（old='' 补丁对已存在文件会失配）。"""
    s0 = _write_stage0(tmp_path)
    state = _factory_env(tmp_path, monkeypatch, s0)
    factory = t7.make_spec_factory("t7l-wiring", "b-code")
    first = factory(state)  # 出生 run1
    Path(first.cmd[1]).write_text("# born\n", encoding="utf-8")
    state2 = dict(state, agent_runs=[{"status": "failed"}])
    spec2 = factory(state2)  # run2 —— 新 workdir，但同理验证语义
    # run2 是新目录：仍应注入（新文件尚未出生）；重入语义针对同一 target。
    assert spec2.extra_prompt_sections
    # 直接验证同 target 重入：已存在则不注入
    Path(spec2.cmd[1]).write_text("# born\n", encoding="utf-8")
    state3 = dict(state, agent_runs=[{"status": "failed"}])
    monkeypatch.setattr(t7, "_libero_run_dir",
                        lambda tid, st, idx: Path(spec2.workdir))
    spec3 = factory(state3)  # run_idx=3 但目录钉在 run2 workdir（target 已存在）
    assert spec3.extra_prompt_sections == ()


def test_factory_d4_degraded_zero_spec(tmp_path, monkeypatch):
    s0 = _write_stage0_degraded(tmp_path)
    state = _factory_env(tmp_path, monkeypatch, s0)
    factory = t7.make_spec_factory("t7l-wiring", "b-code")
    with pytest.raises(GraphStateError, match="D-4"):
        factory(state)


def test_factory_unknown_stage_rejected():
    with pytest.raises(GraphStateError, match="未知 stage"):
        t7.make_spec_factory("t", "b", stage="stage9")


# ---------------------------------------------------------------------------
# ③ evaluate 两级装入 + 引擎组装
# ---------------------------------------------------------------------------

_EVAL_FIELDS = {"ok", "thread_id", "wj_report_path", "schema", "stage",
                "aggregate", "judge_sources_used", "gate2_ready", "provenance",
                # 攒批85：persist rungs 契约对接（能判则判，不可判显式标记）
                "rungs", "rungs_not_judged", "rungs_not_judged_reason"}


def _workdir_with_results(tmp_path: Path, doc: dict | None,
                          *, video: bool = True) -> Path:
    wd = tmp_path / "run_1"
    wd.mkdir(parents=True, exist_ok=True)
    if doc is not None:
        (wd / t7.RESULTS_NAME).write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    if video:
        (wd / t7.VIDEO_NAME).write_bytes(b"\x00fake-mp4")
    (wd / t7.COVER_NAME).write_bytes(b"\x89PNGfake")
    return wd


def test_evaluate_state_key_positive_frozen_fields(tmp_path):
    """state 键正路径：12 字段冻结（攢批85 +rungs 三键；与 seam 测试同口径）。"""
    wd = _workdir_with_results(tmp_path, _valid_results_doc())
    report = t7.build_wj_report(
        thread_id="t7l-wiring", stage=t7.STAGE1_SPATIAL,
        suite_name="libero_spatial", n_episodes_per_task=2,
        tasks=[{"task_id": "t1", "bddl": "libero_spatial/t1.bddl",
                "success": 1, "episodes": 2,
                "metrics_ref": {"path": t7.RESULTS_NAME,
                                "keys": ["tasks"], "sha256": None},
                "judge_source": "官方", "rungs": None,
                "media": [t7.VIDEO_NAME, t7.RESULTS_NAME]}],
        media=[{"kind": "video", "path": t7.VIDEO_NAME, "sha256": None},
               {"kind": "log", "path": t7.RESULTS_NAME, "sha256": None}],
        stage0_compat_ref="evidence/stage0.json", gate2_ready=True)
    wj = wd / t7.WJ_REPORT_NAME
    wj.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    seam = t7.make_evaluate_seam("t7l-wiring")
    result = seam({t7.WJ_REPORT_STATE_KEY: str(wj)})
    assert set(result) == _EVAL_FIELDS
    assert result["ok"] and result["aggregate"]["success"] == 1
    assert result["wj_report_path"] == str(wj)


def test_evaluate_derives_from_telemetry_dir(tmp_path):
    """state 键缺省 → telemetry_uri 同目录 wj_report.json 命中（不触 results）。"""
    wd = _workdir_with_results(tmp_path, None, video=False)
    report = t7.build_wj_report(
        thread_id="t7l-wiring", stage=t7.STAGE1_SPATIAL,
        suite_name="libero_spatial", n_episodes_per_task=2,
        tasks=[{"task_id": "t1", "bddl": "libero_spatial/t1.bddl",
                "success": 0, "episodes": 2,
                "metrics_ref": {"path": t7.RESULTS_NAME,
                                "keys": ["tasks"], "sha256": None},
                "judge_source": "官方", "rungs": None,
                "media": [t7.RESULTS_NAME]}],
        media=[{"kind": "log", "path": t7.RESULTS_NAME, "sha256": None}],
        stage0_compat_ref="evidence/stage0.json", gate2_ready=False)
    (wd / t7.WJ_REPORT_NAME).write_text(
        json.dumps(report, ensure_ascii=False), encoding="utf-8")
    seam = t7.make_evaluate_seam("t7l-wiring")
    result = seam({"agent_runs": [
        {"telemetry_uri": str(wd / t7.TELEMETRY_NAME)}]})
    assert result["wj_report_path"] == str(wd / t7.WJ_REPORT_NAME)
    assert result["gate2_ready"] is False


def test_evaluate_assembles_from_results(tmp_path):
    """主生产路径：只有 results.json → 引擎组装、原子落盘 0600、聚合现算。"""
    wd = _workdir_with_results(tmp_path, _valid_results_doc())
    s0 = str(tmp_path / "stage0.json")
    seam = t7.make_evaluate_seam("t7l-wiring")
    state = {"agent_runs": [{"telemetry_uri": str(wd / t7.TELEMETRY_NAME)}],
             t7.STAGE0_REPORT_STATE_KEY: s0}
    result = seam(state)
    assert set(result) == _EVAL_FIELDS
    wj_path = Path(result["wj_report_path"])
    assert wj_path == wd / t7.WJ_REPORT_NAME
    assert wj_path.is_file()
    assert stat.S_IMODE(wj_path.stat().st_mode) == 0o600
    # 引擎现算：1/2 局成功 = 0.5；官方判分；gate2_ready=true（video 存在）
    assert result["aggregate"] == {"success": 1, "episodes": 2,
                                   "success_rate": 0.5}
    assert result["judge_sources_used"] == ["官方"]
    assert result["gate2_ready"] is True
    # 攒批85：Rung1–3 套件摘要遥测结构性不可判 → 显式标记
    # （persist._rung_from_metrics 依此按核验级 rung=1 落 Gate2 accept 行）
    assert result["rungs"] is None
    assert result["rungs_not_judged"] is True
    doc = json.loads(wj_path.read_text(encoding="utf-8"))
    assert doc["stage0_compat_ref"] == s0
    assert doc["tasks"][0]["metrics_ref"]["sha256"]  # 数字可回指到字节
    assert doc["tasks"][0]["rungs"] is None  # 无 telemetry → 诚实不判
    # 节点可重放：wj_report 已在 → 二级命中，不重写
    before = wj_path.stat().st_mtime_ns
    result2 = seam(state)
    assert result2["wj_report_path"] == str(wj_path)
    assert wj_path.stat().st_mtime_ns == before


def test_evaluate_assembly_failclosed_variants(tmp_path):
    seam = t7.make_evaluate_seam("t7l-wiring")
    s0 = str(tmp_path / "stage0.json")

    def _state(wd):
        return {"agent_runs": [
            {"telemetry_uri": str(wd / t7.TELEMETRY_NAME)}],
            t7.STAGE0_REPORT_STATE_KEY: s0}

    # 缺 results.json
    wd = _workdir_with_results(tmp_path / "a", None)
    with pytest.raises(GraphStateError, match="results.json"):
        seam(_state(wd))
    # 衍生自报字段禁（success_rate）
    wd = _workdir_with_results(
        tmp_path / "b", _valid_results_doc(success_rate=0.5))
    with pytest.raises(GraphStateError, match="衍生字段"):
        seam(_state(wd))
    # 冒充官方：judge 非法枚举
    wd = _workdir_with_results(
        tmp_path / "c", _valid_results_dict(judge="自制"))
    with pytest.raises(GraphStateError, match="judge"):
        seam(_state(wd))
    # 局数失配：episodes != n_episodes_per_task
    wd = _workdir_with_results(
        tmp_path / "d", _valid_results_doc(episodes=5))
    with pytest.raises(GraphStateError, match="episodes"):
        seam(_state(wd))
    # 缺 stage0 溯源键
    wd = _workdir_with_results(tmp_path / "e", _valid_results_doc())
    with pytest.raises(GraphStateError, match="stage0_report_path"):
        seam({"agent_runs": [
            {"telemetry_uri": str(wd / t7.TELEMETRY_NAME)}]})
    # 双缺：无 state 键且无 telemetry
    with pytest.raises(GraphStateError, match="无法定位"):
        seam({})


def _valid_results_dict(**kw) -> dict:
    doc = _valid_results_doc()
    doc.update(kw)
    return doc


def test_evaluate_state_key_missing_file_loud(tmp_path):
    """state 键一旦给出：缺件响亮报错，不静默降级改道。"""
    seam = t7.make_evaluate_seam("t7l-wiring")
    with pytest.raises(GraphStateError, match="不存在"):
        seam({t7.WJ_REPORT_STATE_KEY: str(tmp_path / "nope.json")})


# ---------------------------------------------------------------------------
# ④ Gate1 接缝 + client 方法
# ---------------------------------------------------------------------------

class _FakeFeishu:
    def __init__(self):
        self.calls: list[dict] = []

    def request_gate1_libero_approval(self, thread_id, task_title, proposals,
                                      *, stage0_facts, max_wait=None):
        self.calls.append({"thread_id": thread_id, "task_title": task_title,
                           "proposals": proposals, "stage0_facts": stage0_facts})
        return {"decision": "approve", "choice": 0, "human_notes": "批"}


def test_gate1_seam_compatible_sends_libero_card(tmp_path):
    s0 = _write_stage0(tmp_path)
    fake = _FakeFeishu()
    seam = t7.make_gate1_seam(fake, "t7l-wiring", "Task7 LIBERO Stage1")
    out = seam(_state(s0))
    assert out["decision"] == "approve"
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["stage0_facts"]["outcome"] == "compatible"
    assert call["stage0_facts"]["propose_run"] is True
    assert call["proposals"][0]["name"] == "libero_spatial 最小闭环"


def test_gate1_seam_d4_zero_card(tmp_path):
    s0 = _write_stage0_degraded(tmp_path)
    fake = _FakeFeishu()
    seam = t7.make_gate1_seam(fake, "t7l-wiring", "Task7 LIBERO Stage1")
    with pytest.raises(GraphStateError, match="D-4"):
        seam(_state(s0))
    assert fake.calls == []  # 零卡片：绝不把降级伪装成运行案


def test_client_gate1_libero_send_and_rehang(tmp_path):
    from orchestrator.feishu_gateway.client import FeishuGateway
    gw = FeishuGateway("cli_x", "secret_x", "ou_owner",
                       db_path=tmp_path / "state" / "pending.db",
                       evidence_dir=tmp_path / "ev", run_dir=str(tmp_path),
                       start_ws=False)
    sent: list[dict] = []

    def _fake_send(open_id, card_v1, card_v2, *, form_required=True):
        sent.append({"v1": card_v1, "v2": card_v2})
        return {"message_id": "om_msg_libero_1", "chat_id": "oc_chat_1",
                "schema": "2.0"}

    gw._send_interactive = _fake_send
    gw._await_decision = lambda tid, gate, max_wait=None: {
        "decision": "approve", "choice": 0, "human_notes": "真人批"}
    facts = {"outcome": "compatible", "stage0_schema": "s1",
             "venv_path": "/venv", "pin_facts": {}, "reasons": [],
             "notes": [], "tour_dir": "/tour"}
    props = [{"name": "p1"}]
    out = gw.request_gate1_libero_approval(
        "t7l-client", "Task7 LIBERO", props, stage0_facts=facts)
    assert out["decision"] == "approve"
    assert len(sent) == 1
    # 2.0 主卡带 Stage0 插页与 LIBERO 专属标题
    body = sent[0]["v2"]["body"]["elements"]
    assert "Stage0 兼容事实" in body[1]["text"]["content"]
    assert "LIBERO" in sent[0]["v2"]["header"]["title"]["content"]
    assert "Stage0 兼容事实" in sent[0]["v1"]["elements"][1]["content"]
    row = gw.store.get_pending("t7l-client", 1)
    assert row["card_message_id"] == "om_msg_libero_1"
    # 重挂：PENDING 行 + 已发卡 → 不重发
    gw._send_interactive = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("重挂不得重发卡片"))
    gw._await_decision = lambda tid, gate, max_wait=None: {
        "decision": "approve", "choice": 0, "human_notes": "重挂后批"}
    out2 = gw.request_gate1_libero_approval(
        "t7l-client", "Task7 LIBERO", props, stage0_facts=facts)
    assert out2["decision"] == "approve"


# ---------------------------------------------------------------------------
# ⑤ 卡片结构 + Gate2 三问
# ---------------------------------------------------------------------------

def _facts() -> dict:
    return {"outcome": "compatible",
            "stage0_schema": "simpraxis.task7.stage0_compat.v1",
            "venv_path": "/home/agent/runs/phase2/venvs/libero",
            "pin_facts": {"numpy_is_shared_pin": True,
                          "torch_is_shared_pin": True,
                          "mujoco_is_shared_pin": True,
                          "mujoco_diverged": False},
            "reasons": ["robosuite ok"], "notes": [],
            "tour_dir": "/mnt/tours/libero"}


def test_gate1_card_libero_structure():
    from orchestrator.feishu_gateway import cards
    props = [{"name": "p1", "sim_engine": "LIBERO", "first_principles": "x",
              "artifacts": ["video.mp4"]}]
    c2 = cards.build_gate1_card_libero("T7", props, _facts())
    assert c2["schema"] == "2.0"
    assert "Stage0 已证兼容" in c2["header"]["title"]["content"]
    el = c2["body"]["elements"]
    assert "Stage0 兼容事实" in el[1]["text"]["content"]
    assert "outcome：**compatible**" in el[1]["text"]["content"]
    assert "方案 1" in el[2]["text"]["content"]  # 方案 div 紧随其后
    c1 = cards.build_gate1_card_libero_v1("T7", props, _facts())
    assert "Stage0 兼容事实" in c1["elements"][1]["content"]
    assert c1["header"]["title"]["content"] == c2["header"]["title"]["content"]
    # 表单仍可交互（批准/打回按钮 value 结构不破）
    btns = [x for x in el if x.get("tag") == "form"][0]["elements"]
    assert btns[1]["value"] == {"gate": 1, "decision": "approve", "choice": 0}


def test_gate2_metrics_wj_branch_and_legacy():
    from orchestrator.feishu_gateway import cards
    md = cards._gate2_metrics_md("T", {
        "wj_report_path": "/runs/wj_report.json",
        "aggregate": {"success": 1, "episodes": 2, "success_rate": 0.5},
        "stage": "stage1_spatial", "judge_sources_used": ["官方"],
        "gate2_ready": True,
    }, None)
    assert "WJ-Lite" in md and "1/2 局成功" in md and "0.500" in md
    assert "stage1_spatial" in md and "官方" in md
    # 旧路径不破（Task3 合成 Gate2 仍用扁平键）
    md2 = cards._gate2_metrics_md("T", {"pose_error": 0.1}, None)
    assert "位姿误差" in md2
    # rungs 嵌套路径不破（Phase1 task7）
    md3 = cards._gate2_metrics_md("T", {
        "rungs": {"rung1": {"pass": True, "checks": {}},
                  "rung2": {"pass": True, "checks": {}},
                  "rung3": {"pass": True, "checks": {}},
                  "rung4": None}}, None)
    assert "Rung" in md3


def test_gate2_explanation_libero(tmp_path):
    from orchestrator.feishu_gateway import cards  # noqa: F401  (键家法同源)
    wd = _workdir_with_results(tmp_path, _valid_results_doc())
    report = t7.build_wj_report(
        thread_id="t7l-wiring", stage=t7.STAGE1_SPATIAL,
        suite_name="libero_spatial", n_episodes_per_task=2,
        tasks=[{"task_id": "t1", "bddl": "libero_spatial/t1.bddl",
                "success": 1, "episodes": 2,
                "metrics_ref": {"path": t7.RESULTS_NAME,
                                "keys": ["tasks"], "sha256": None},
                "judge_source": "官方", "rungs": None,
                "media": [t7.VIDEO_NAME, t7.RESULTS_NAME]}],
        media=[{"kind": "video", "path": t7.VIDEO_NAME, "sha256": None},
               {"kind": "log", "path": t7.RESULTS_NAME, "sha256": None}],
        stage0_compat_ref="evidence/stage0.json", gate2_ready=True)
    wj = wd / t7.WJ_REPORT_NAME
    wj.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    (wd / t7.MANIFEST_NAME).write_text(
        json.dumps({"n_frames": 240, "frame_dt_s": 0.04}), encoding="utf-8")
    state = {"chosen": {"name": "libero_spatial 最小闭环"},
             "metrics": {"wj_report_path": str(wj)},
             "telemetry_uri": str(wd / t7.TELEMETRY_NAME)}
    expl = t7.build_gate2_explanation(state, {"duration_ms": 9600})
    assert set(expl) == {"task", "problem", "video"}
    assert "libero_spatial 最小闭环" in expl["task"]
    assert "1/2 局成功" in expl["problem"]
    assert "240 帧" in expl["video"] and "9.6 s" in expl["video"]
    # 缺报表：退回纯叙事不编数字（返回 dict 但无聚合行）
    expl2 = t7.build_gate2_explanation({"chosen": {}}, {})
    assert set(expl2) == {"task", "problem", "video"}
    assert "本次聚合" not in expl2["problem"]


# ---------------------------------------------------------------------------
# ⑥ prompts 模块纯函数
# ---------------------------------------------------------------------------

def test_prompts_birth_contract_and_facts():
    from orchestrator.graph import task7_libero_prompts as prompts
    body = prompts.stage_birth_contract(
        stage="stage1_spatial", script_name="stage1_minimal.py",
        n_episodes=2, stage0_facts_text="outcome=compatible")
    assert "stage1_minimal.py" in body
    assert '"old":""' in body.replace(" ", "")  # 单次 JSON 补丁出生语义
    assert "libero_official" in body or "自补" in body
    assert "outcome=compatible" in body
    facts = prompts.stage0_facts_text(_state(Path("/s0.json"), **{})[
        "chosen"] | {"outcome": "compatible", "pin_facts": {}, "reasons": [],
                     "notes": [], "tour_dir": "/t", "venv_path": "/v",
                     "stage0_schema": "s"})
    assert "outcome=compatible" in facts and "tour_dir=/t" in facts
