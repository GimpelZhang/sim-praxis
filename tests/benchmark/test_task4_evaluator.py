"""Phase 2.8.0 Task 4 机判：AC-T4-1..8（Independent Evaluator 与评分体系）。

纪律（硬规则 7 / T4.1 不变量）：

- **不 mock 评分逻辑**：所有产物都是真实写盘的结构化文件，评测器真读快照、
  真从 telemetry 重算；「伪造」用例只能改候选产物，改不动评测器；
- **权重与阈值只来自 ``criteria.json``**：用例里的数值阈值一律从冻结判据读，
  不在测试里复制字面量（AC-T4-7 另有 AST 扫描守住 scoring.py）；
- **不碰仓内文件**：AC-T4-5 在 ``tmp_path`` 的副本上改评测器源码，测完即弃；
- **独立进程**：AC-T4-8 用 ``subprocess`` 跑 ``scripts/benchmark/evaluate_run.py``。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.benchmark import evaluator as ev
from orchestrator.benchmark import result_schema as rs
from orchestrator.benchmark import runtime_fingerprint as rf
from orchestrator.benchmark import scoring
from orchestrator.benchmark import task_spec as ts
from orchestrator.benchmark.registry import BenchmarkRegistry

_REPO = Path(__file__).resolve().parents[2]
_ZERO_SHA = "0" * 64


# --------------------------------------------------------------------------
# 产物包构造（合成产物；Task 6 才用真实参考执行）
# --------------------------------------------------------------------------

def _registry() -> BenchmarkRegistry:
    return BenchmarkRegistry()


def _spec(task_id: str) -> ts.TaskSpec:
    return _registry().get(task_id)


def _threshold(spec: ts.TaskSpec, name: str) -> float:
    return float(spec.thresholds[name]["value"])


def _encode(payload) -> bytes:
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n").encode("utf-8")


def _write(run_dir: Path, files: "dict[str, object]") -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    for rel, payload in files.items():
        path = run_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_encode(payload))
    return run_dir


def _digest_map(files: "dict[str, object]", outputs: "tuple[str, ...]") -> dict:
    return {rel: hashlib.sha256(_encode(files[rel])).hexdigest()
            for rel in outputs if rel != "run_manifest.json" and rel in files}


def _manifest(spec: ts.TaskSpec, files: "dict[str, object]", *,
              run_id: str, mode: str = "reference",
              overrides: "dict | None" = None) -> dict:
    doc = {
        "schema": "simpraxis.benchmark_run.v1",
        "run_id": run_id,
        "mode": mode,
        "task_id": spec.task_id,
        "task_version": spec.version,
        "repository_sha": "1" * 40,
        "runtime_fingerprint_sha256": hashlib.sha256(
            _encode(files["runtime_fingerprint.json"])).hexdigest(),
        "task_spec_sha256": spec.task_file_sha256,
        "criteria_sha256": spec.criteria_sha256,
        "criteria_lock_sha256": spec.criteria_lock_sha256,
        "seed": 0,
        "candidate_artifact_sha256": rs.candidate_digest(
            _digest_map(files, spec.outputs)),
        "birth_result_sha256": (
            hashlib.sha256(_encode(files["birth/functional_birth.json"])).hexdigest()
            if "birth/functional_birth.json" in files else None),
        "evaluator_version": ev.EVALUATOR_VERSION,
        "status": "PASS",
        "paid_model_calls": 0,
        "evidence_manifest_sha256": None,
    }
    doc.update(overrides or {})
    return doc


def _fingerprint(spec: ts.TaskSpec, *, backend: str, packages: dict,
                 simulator: "dict | None") -> dict:
    """合成一份**过 schema** 的运行指纹（评审 W3-02 起评测器会逐份校验）。

    形状与 ``runtime_fingerprint.v1.schema.json`` 的 required 一一对应；自哈希
    用与生产同一函数 ``rf.fingerprint_sha256`` 计算，不走捷径。
    """
    is_host = backend == "host"
    digest = spec.runtime["image_digest"]
    doc = {
        "schema": "simpraxis.runtime_fingerprint.v1",
        "captured_in": "host" if is_host else "container",
        "task_id": spec.task_id,
        "runtime_id": spec.runtime["runtime_id"],
        "repository": {
            "engine_head": "1" * 40, "engine_dirty": False,
            "kb_head": None, "kb_dirty": None,
            "benchmark_version": spec.version, "task_version": spec.version,
            "task_spec_sha256": spec.task_file_sha256,
            "criteria_sha256": spec.criteria_sha256,
            "assets_manifest_sha256": None,
            "skills_manifest_sha256": None,
        },
        "container": {
            "backend": backend,
            "image_name": None if is_host else "libero_isolated",
            "image_digest": digest, "image_id": digest,
            "container_id": None,
            "container_python": None if is_host else spec.runtime["python"],
            "uid": 1000, "gid": 1000, "kernel": "6.8.0-test",
            "gpu_visible": False,
            "cuda_runtime": None,
            "egl_available": None if is_host else True,
        },
        "python": {
            "executable": spec.runtime["python"], "version": "3.10.12",
            "implementation": "CPython", "sys_path": [],
            "workdir": str(_REPO), "packages": dict(packages), "argv": [],
        },
        "mount": {"entries": []},
        "config": {"env_passthrough": [], "env": {}, "network_mode": "disabled",
                   "timeout_s": 600, "cpu_limit": None, "memory_limit": None,
                   "gpu": {"name": None, "total_mib": None}},
        "simulator": simulator,
    }
    doc["fingerprint_sha256"] = rf.fingerprint_sha256(doc)
    return doc


def _mujoco_simulator(spec: ts.TaskSpec) -> dict:
    return {
        "name": "mujoco",
        "version": str(spec.runtime["packages"]["mujoco"]),
        "api_identity": "mujoco.MjModel",
        "observation_contract": {"keys": ["cube_pos", "target_pos"],
                                 "dtype": "float64", "shape": [6]},
        "action_contract": {"space": "Box", "dim": 3,
                            "low": [-1, -1, -1], "high": [1, 1, 1]},
        "scene_identity": "synthetic_push_v1",
    }


def _host_fingerprint(spec: ts.TaskSpec, packages: dict) -> dict:
    simulator = None if spec.task_id == "icy_ground_dr_v1" \
        else _mujoco_simulator(spec)
    return _fingerprint(spec, backend="host", packages=packages,
                        simulator=simulator)


def _container_fingerprint(spec: ts.TaskSpec) -> dict:
    return _fingerprint(
        spec, backend="docker",
        packages={name: str(value) for name, value
                  in spec.runtime["packages"].items()},
        simulator={
            "name": "robosuite",
            "version": str(spec.runtime["packages"]["robosuite"]),
            "api_identity": "robosuite.make",
            "observation_contract": {"keys": ["obs", "state"],
                                     "dtype": "float64", "shape": [2]},
            "action_contract": {"space": "Box", "dim": 7,
                                "low": [-1] * 7, "high": [1] * 7},
            "scene_identity": "KITCHEN_SCENE",
        })


def _mujoco_files(*, steps: "int | None" = None, endpoint_ratio: float = 0.9,
                  penetration_ratio: float = 0.4, force_ratio: float = 0.3,
                  same_seed: bool = True, cross_delta_ratio: float = 0.2,
                  metrics_endpoint: "float | None" = None,
                  fingerprint_packages: "dict | None" = None,
                  hidden_reads: "list[str] | None" = None,
                  metrics_override: "dict | None" = None,
                  split_ratio: float = 0.1) -> "dict[str, object]":
    spec = _spec("mujoco_contact_push_v1")
    d_max = _threshold(spec, "endpoint_distance_m")
    pen_max = _threshold(spec, "max_penetration_m")
    f_min = _threshold(spec, "contact_force_peak_n_min")
    f_max = _threshold(spec, "contact_force_peak_n_max")
    steps_min = int(_threshold(spec, "steps_min"))
    tol = _threshold(spec, "repro_abs_tol")
    n_steps = steps or (steps_min + 66)
    end_x = d_max * endpoint_ratio
    pen = pen_max * penetration_ratio
    force = f_min + (f_max - f_min) * force_ratio
    start_x = -(end_x + 0.05)

    telemetry = []
    for index in range(n_steps):
        frac = index / (n_steps - 1) if n_steps > 1 else 1.0
        x = start_x + (end_x - start_x) * frac
        telemetry.append({
            "step": index,
            "t": round(index * 0.002, 6),
            "cube_pos": [round(x, 9), 0.0, 0.025],
            "target_pos": [0.0, 0.0, 0.025],
            "max_penetration": pen,
            "contact_force_norm": force,
        })
    recommitted = scoring.mujoco_facts({"telemetry.jsonl": telemetry},
                                       spec.criteria)["recomputed"]
    metrics = dict(recommitted)
    if metrics_endpoint is not None:
        metrics["endpoint_distance_m"] = metrics_endpoint

    #: 评审 W3-10：复现结论必须能由两次运行各自的原始件重算。这里按判据
    #: 造出同种子重跑（逐字段相同，或按 ``same_seed`` 扰动一行末位）与换种子
    #: 运行（端点整体平移 ``tol * cross_delta_ratio``）。
    delta = tol * cross_delta_ratio
    same_rows = [dict(row) for row in telemetry]
    if not same_seed:
        same_rows[0]["cube_pos"] = [same_rows[0]["cube_pos"][0] + 1e-9,
                                    *same_rows[0]["cube_pos"][1:]]
    cross_rows = [dict(row, cube_pos=[row["cube_pos"][0] + delta,
                                      *row["cube_pos"][1:]])
                  for row in telemetry]
    cross_metrics = dict(recommitted)
    cross_metrics["endpoint_distance_m"] = round(
        float(recommitted["endpoint_distance_m"]) + delta, 9)

    def _lines(rows) -> str:
        return "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)

    files: "dict[str, object]" = {
        "solution.py": "def build_scene():\n    return {}\n",
        "scene_spec.json": {"scene": "push", "block_half_size_m": 0.025},
        "metrics.json": metrics,
        "telemetry.jsonl": _lines(telemetry),
        "runtime_fingerprint.json": _host_fingerprint(
            spec, fingerprint_packages
            or {name: str(value) for name, value
                in spec.runtime["packages"].items()}),
        "reproducibility.json": {
            "same_seed_identical": same_seed,
            "cross_seed_max_delta_m": delta,
        },
        "hidden_access_audit.json": {"read": list(hidden_reads or []),
                                     "written": [], "modified": []},
        "repro/same_seed/metrics.json": dict(recommitted),
        "repro/same_seed/telemetry.jsonl": _lines(same_rows),
        "repro/cross_seed/metrics.json": cross_metrics,
        "repro/cross_seed/telemetry.jsonl": _lines(cross_rows),
    }
    if metrics_override is not None:
        # 评审 CR-32 用例：允许构造「键不全」的 metrics.json，且 manifest
        # 的候选聚合按修改后的内容计算（不能事后改写而让 sha 失配）。
        files["metrics.json"] = dict(metrics_override)
    else:
        files["metrics.json"]["steps"] = int(files["metrics.json"]["steps"])
    return files


def _mujoco_bundle(run_dir: Path, *, run_id: str = "m1-golden",
                   manifest: "dict | None" = None, **kwargs) -> Path:
    spec = _spec("mujoco_contact_push_v1")
    files = _mujoco_files(**kwargs)
    files["run_manifest.json"] = _manifest(spec, files, run_id=run_id,
                                           overrides=manifest)
    return _write(run_dir, files)


# -- L1 ---------------------------------------------------------------------

_L1_ADAPTER = '''\
"""LIBERO 适配器（Task 4 评测器用的合成样本；不 import 任何真实引擎）。"""


def build_env():
    return {"action_dim": 7}


def reset(env):
    return {"obs": 0.0, "state": 0.0}


def step(env, action):
    return {"obs": 0.0, "state": 0.0}
'''


def _libero_files(*, b2_status: str = "PASS", advanced: bool = True,
                  probe_action_dim: int = 7,
                  probe_python: str = "/usr/bin/python3",
                  adapter_source: str = _L1_ADAPTER,
                  hidden_reads: "list[str] | None" = None) -> "dict[str, object]":
    spec = _spec("libero_env_adapter_v1")
    fingerprint = _container_fingerprint(spec)
    levels = {name: {"status": "PASS", "reason": None, "detail": "ok"}
              for name in ("B0", "B1", "B2", "B3", "B4")}
    levels["B2"] = {"status": b2_status, "reason": None, "detail": "ok"}
    return {
        "adapter.py": adapter_source,
        "env_probe.json": {
            "captured_in": "container",
            "python_executable": probe_python,
            "python_version": "3.10.12",
            "image_digest": spec.runtime["image_digest"],
            "action_dim": probe_action_dim,
            "observation_keys": ["obs", "state"],
            "env_name": "KITCHEN_SCENE",
            "task_suite": "libero_spatial",
        },
        "one_step_result.json": {"advanced": advanced,
                                 "state": {"obs": 0.5, "state": 0.25}},
        "runtime_fingerprint.json": fingerprint,
        "birth/functional_birth.json": {
            "schema": "simpraxis.benchmark_birth.v1",
            "task_id": spec.task_id, "birth_status": "BORN",
            "levels": levels, "problems": [], "task_success": None,
        },
        "hidden_access_audit.json": {"read": list(hidden_reads or []),
                                     "written": [], "modified": []},
    }


def _libero_bundle(run_dir: Path, *, run_id: str = "l1-golden",
                   manifest: "dict | None" = None, **kwargs) -> Path:
    spec = _spec("libero_env_adapter_v1")
    files = _libero_files(**kwargs)
    files["run_manifest.json"] = _manifest(spec, files, run_id=run_id,
                                           overrides=manifest)
    return _write(run_dir, files)


# -- R1 ---------------------------------------------------------------------

_R1_PLAN = """\
research_problem:
  question: 冰面域随机化对推箱策略鲁棒性的影响（仅设计，不执行训练）
  motivation: 历史 Q1 给出负结论，本计划只检验随机化设计是否可机检
  prior_evidence: Phase 1 Q1（ice/normal=0.824<2.0）作为先验证据引用
  rl_constraints_unchanged: true
parameter_definitions:
  - {name: friction, unit: "1", range: [0.05, 0.95],
     rationale: 冰面/干面摩擦实测区间，覆盖滑与涩两端}
  - {name: contact_stiffness, unit: N/m, range: [500, 5000],
     rationale: 低温硬化使接触刚度跨一个数量级}
  - {name: joint_damping, unit: N*m*s/rad, range: [0.1, 2.0],
     rationale: 常见机器人关节阻尼参数带}
probability_distributions:
  friction: {family: uniform, low: 0.05, high: 0.95, support: [0.05, 0.95],
             rationale: 摩擦无先验方向，均匀分布取最大熵}
  contact_stiffness: {family: log-uniform, low: 500, high: 5000,
                      support: [500, 5000],
                      rationale: 刚度跨一个数量级，对数均匀避免样本挤在上界}
  joint_damping: {family: uniform, low: 0.1, high: 2.0, support: [0.1, 2.0],
                  rationale: 阻尼在区间内近似线性可调}
parameter_correlations:
  - {pair: [friction, contact_stiffness], rationale: 冰面湿滑与接触刚度同源物理耦合}
randomization_method: 每次 episode 开始时按类别独立采样
sampling_strategy: 拉丁超立方 + 分层覆盖
curriculum: 第 1 阶段低摩擦权重 0.5，第 2 阶段线性升至 0.9
dataset_splits:
  disjoint: true
  train: {ids: [e1, e2, e3], episodes: 3}
  validation: {ids: [v1, v2]}
  holdout: {ids: [h1, h2]}
random_seeds: [0, 1, 2]
metrics:
  - {name: success_rate, maps_to: metrics}
  - {name: ice_ratio, maps_to: parameter_definitions}
ablation_study:
  - {name: no_dr, removes: randomization_method}
  - {name: no_iciness, removes: probability_distributions}
  - {name: no_curriculum, removes: curriculum}
failure_taxonomy:
  slip_off: 冰面滑出目标区
  stuck: 接触力不足无法推动
sim_to_real_risks:
  - 真实冰面摩擦系数分布未知，仿真区间可能过窄
conclusion_criteria:
  rule: 若 holdout 成功率相对基线的提升 <5% 则判不成立
  evidence_refs: [metrics, dataset_splits, ablation_study]
"""

_R1_REPORT = """\
# 冰面域随机化研究设计（设计文档，不执行训练）

本方案只给出可机检的实验设计，不产生新实验数据，也不对历史结论作任何改判声明。
"""


def _research_files(*, drop_section: "str | None" = None,
                    missing_unit: bool = False,
                    overlap_holdout: bool = False,
                    duplicate_holdout: bool = False,
                    disjoint: bool = True,
                    claimed_success: bool = False,
                    criterion_met: bool = False,
                    report: str = _R1_REPORT) -> "dict[str, object]":
    spec = _spec("icy_ground_dr_v1")
    plan = _yaml_load(_R1_PLAN)
    if drop_section:
        plan.pop(drop_section)
    splits = plan.get("dataset_splits")
    if isinstance(splits, dict):
        if overlap_holdout:
            splits["holdout"]["ids"] = ["h1", "e1"]
        if duplicate_holdout:
            # 评审 CR-60：声明 2 个 holdout episode，但只有 1 个唯一 id。
            splits["holdout"]["ids"] = ["h1", "h1"]
        splits["disjoint"] = disjoint
    params = [
        {"name": "friction", "class": "friction", "unit": "1" if not missing_unit else ""},
        {"name": "stiffness", "class": "contact_stiffness", "unit": "N/m"},
        {"name": "damping", "class": "joint_damping", "unit": "N*m*s/rad"},
    ]
    return {
        "experiment_plan.yaml": plan,
        "research_report.md": report,
        "parameter_space.json": {"parameters": params},
        "evaluation_metrics.json": {
            "metrics": [{"name": "success_rate", "statistical_test": "Welch t",
                         "value": 0.5, "ci": [0.4, 0.6]}],
            "conclusion": {"criterion_met": criterion_met,
                           "claimed_success": claimed_success},
        },
        "runtime_fingerprint.json": _host_fingerprint(
            spec, {name: str(value) for name, value
                   in spec.runtime["packages"].items()}),
    }


def _research_bundle(run_dir: Path, *, run_id: str = "r1-golden",
                     manifest: "dict | None" = None, **kwargs) -> Path:
    spec = _spec("icy_ground_dr_v1")
    files = _research_files(**kwargs)
    files["run_manifest.json"] = _manifest(spec, files, run_id=run_id,
                                           overrides=manifest)
    return _write(run_dir, files)


def _yaml_load(text: str) -> dict:
    import yaml
    return yaml.safe_load(text)


# --------------------------------------------------------------------------
# 评测调用
# --------------------------------------------------------------------------

def _evaluate(task_id: str, run_dir: Path, out_dir: Path, *,
              source_root: "Path | None" = None) -> dict:
    spec = _spec(task_id)
    evaluator = ev.Evaluator(spec=spec, source_root=source_root)
    return evaluator.evaluate(run_dir, out_dir)


def _load_result(out_dir: Path) -> dict:
    return json.loads((out_dir / rs.EVALUATION_NAME).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# AC-T4-1 合法产物 → PASS，维度在 [0,1]，hard_gates 为空
# --------------------------------------------------------------------------

def test_ac_p0_4_1_pass(tmp_path: Path):
    run_dir = _mujoco_bundle(tmp_path / "run")
    out_dir = tmp_path / "eval"
    written = _evaluate("mujoco_contact_push_v1", run_dir, out_dir)
    assert written["status"] == "PASS", written
    doc = _load_result(out_dir)
    assert doc["status"] == "PASS"
    assert doc["hard_gates"] == []
    assert doc["weighted_score"] == pytest.approx(1.0)
    assert set(doc["dimensions"]) == set(_spec(
        "mujoco_contact_push_v1").weights)
    for name, dim in doc["dimensions"].items():
        assert 0.0 <= dim["score"] <= 1.0, (name, dim)
        assert 0.0 <= dim["weight"] <= 1.0, (name, dim)
        assert dim["detail"], name
    # 侧车 sha 与文件逐字节相符
    sidecar = (out_dir / rs.EVALUATION_SHA_NAME).read_text(
        encoding="utf-8").split()
    assert sidecar == [doc_sha(out_dir), rs.EVALUATION_NAME]
    # 快照与重算轨迹落盘（T4.7）
    snap = out_dir / ev.SNAPSHOT_DIR / ev.SNAPSHOT_MANIFEST
    assert snap.is_file()
    assert (out_dir / ev.TRACE_NAME).is_file()
    trace = (out_dir / ev.TRACE_NAME).read_text(encoding="utf-8").splitlines()
    assert len(trace) == len((run_dir / "telemetry.jsonl").read_text(
        encoding="utf-8").splitlines())
    # 候选目录里的文件与快照逐字节相同（快照是复制而非引用）
    assert (out_dir / ev.SNAPSHOT_DIR / "metrics.json").read_bytes() == \
        (run_dir / "metrics.json").read_bytes()


def doc_sha(out_dir: Path) -> str:
    return hashlib.sha256((out_dir / rs.EVALUATION_NAME).read_bytes()).hexdigest()


# --------------------------------------------------------------------------
# AC-T4-2 七个 Hard Gate 各触发一次均得到预期状态
# --------------------------------------------------------------------------

def _tampered_source_root(tmp_path: Path) -> Path:
    root = tmp_path / "src"
    for rel in ev.EVALUATOR_SOURCES:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_REPO / rel, target)
    evaluator_path = root / "orchestrator/benchmark/evaluator.py"
    evaluator_path.write_text(evaluator_path.read_text(encoding="utf-8")
                              + "\n# tampered for AC-T4-5\n", encoding="utf-8")
    return root


@pytest.mark.parametrize("case", [
    ("ev1_runtime", "INVALID_RUNTIME", "HG-EVAL-1"),
    ("ev2_hidden", "FAIL", "HG-EVAL-2"),
    ("ev3_physics", "FAIL", "HG-EVAL-3"),
    ("ev4_repro", "FAIL", "HG-EVAL-4"),
    ("ev5_missing", "INCOMPLETE_EVIDENCE", "HG-EVAL-5"),
    ("ev6_artifact", "INVALID_ARTIFACT", "HG-EVAL-6"),
    ("ev7_selfcheck", "BLOCKED_INFRA", "HG-EVAL-7"),
])
def test_ac_p0_4_2_hard_gates(tmp_path: Path, case):
    name, expected_status, gate = case
    run_dir = tmp_path / "run"
    out_dir = tmp_path / "eval"
    source_root = None
    if name == "ev1_runtime":
        _mujoco_bundle(run_dir, fingerprint_packages={
            "mujoco": "2.3.7", "numpy": "1.26.4", "trimesh": "5.1.1"})
    elif name == "ev2_hidden":
        _mujoco_bundle(run_dir,
                       hidden_reads=["private/hidden/mujoco_seeds.json"])
    elif name == "ev3_physics":
        spec = _spec("mujoco_contact_push_v1")
        _mujoco_bundle(run_dir, penetration_ratio=2.5,
                       metrics_endpoint=_threshold(spec, "endpoint_distance_m")
                       * 0.9)
    elif name == "ev4_repro":
        _mujoco_bundle(run_dir, same_seed=False)
    elif name == "ev5_missing":
        spec = _spec("mujoco_contact_push_v1")
        files = _mujoco_files()
        files.pop("metrics.json")
        files["run_manifest.json"] = _manifest(
            spec, {**files, "metrics.json": b""}, run_id="m1-missing")
        _write(run_dir, files)
    elif name == "ev6_artifact":
        _mujoco_bundle(run_dir, manifest={"candidate_artifact_sha256": "f" * 64})
    elif name == "ev7_selfcheck":
        _mujoco_bundle(run_dir)
        source_root = _tampered_source_root(tmp_path)
    written = _evaluate("mujoco_contact_push_v1", run_dir, out_dir,
                        source_root=source_root)
    assert written["status"] == expected_status, (name, written)
    assert gate in written["hard_gate_ids"], (name, written["hard_gate_ids"])
    assert gate in written["gates_checked"]
    doc = _load_result(out_dir)
    assert doc["status"] == expected_status


# --------------------------------------------------------------------------
# W3-12 阈值边界：等号必须判过，越界一丁点必须判负
# --------------------------------------------------------------------------

def test_w3_12_threshold_boundaries(tmp_path: Path):
    """评审 W3-12：把 ``<=`` 钉死。

    早先没有「恰好等于阈值」的用例：把 ``endpoint_distance_m <= d_max`` 写成
    ``<``、或把 HG-M1-3 的 ``>`` 写成 ``>=``，测试仍会全绿。这里四类阈值同时
    取等号（端距 / 穿模 / 接触力下界 / 步数下界）必须满分通过，随后每一类各
    越界一丁点必须判负——gt/le 的语义由行为钉住，不靠读代码。
    """
    spec = _spec("mujoco_contact_push_v1")
    pen_max = _threshold(spec, "max_penetration_m")
    steps_min = int(_threshold(spec, "steps_min"))

    # (a) 恰好等于阈值 → PASS，四维全 1.0，HG-M1-3 不触发。
    #     端距阈值是 10 位小数（0.0146446609），重算值按 9 位四舍五入后**无法**
    #     精确等于它，所以端距取「刚好在阈值内侧」（ratio<1）；穿模（0.001）与
    #     接触力下界（0.2943）在 9 位上可精确命中，用来钉住``<=`` 的等号行为。
    at_run = _mujoco_bundle(tmp_path / "at", endpoint_ratio=0.999999,
                            penetration_ratio=1.0, force_ratio=0.0,
                            steps=steps_min)
    at_out = tmp_path / "at_eval"
    at = _evaluate("mujoco_contact_push_v1", at_run, at_out)
    assert at["status"] == "PASS", at
    assert "HG-M1-3" not in at["hard_gate_ids"], at["hard_gate_ids"]
    at_doc = _load_result(at_out)
    for name, dim in at_doc["dimensions"].items():
        assert dim["score"] == pytest.approx(1.0), (name, dim)

    # (b) 穿模越过上限一点点（0.001 → 0.001000001）→ HG-M1-3 触发、判负
    over_run = _mujoco_bundle(tmp_path / "over", endpoint_ratio=0.999999,
                              penetration_ratio=1.0 + 1e-6,
                              force_ratio=0.0, steps=steps_min)
    over = _evaluate("mujoco_contact_push_v1", over_run, tmp_path / "over_eval")
    assert over["status"] == "FAIL", over
    assert "HG-M1-3" in over["hard_gate_ids"], over["hard_gate_ids"]
    over_doc = _load_result(tmp_path / "over_eval")
    assert over_doc["dimensions"]["physics_validity"]["score"] < 1.0
    hit = [g for g in over_doc["hard_gates"] if g["id"] == "HG-M1-3"]
    assert hit and "vs 上限" in hit[0]["detail"], over_doc["hard_gates"]

    # (c) 端距越过上限一点点 → 功能维度失分、整体不得 PASS
    far_run = _mujoco_bundle(tmp_path / "far", endpoint_ratio=1.0 + 1e-4,
                             penetration_ratio=1.0, force_ratio=0.0,
                             steps=steps_min)
    far = _evaluate("mujoco_contact_push_v1", far_run, tmp_path / "far_eval")
    assert far["status"] != "PASS", far

    # (d) 步数少一步（steps_min-1）→ 功能维度失分、整体不得 PASS
    short_run = _mujoco_bundle(tmp_path / "short", endpoint_ratio=0.999999,
                               penetration_ratio=1.0, force_ratio=0.0,
                               steps=steps_min - 1)
    short = _evaluate("mujoco_contact_push_v1", short_run,
                      tmp_path / "short_eval")
    assert short["status"] != "PASS", short


# --------------------------------------------------------------------------
# AC-T4-3 伪造 metrics → FAIL，且重算值与伪造值不同
# --------------------------------------------------------------------------
def test_ac_p0_4_3_forged_metrics(tmp_path: Path):
    spec = _spec("mujoco_contact_push_v1")
    forged = _threshold(spec, "endpoint_distance_m") * 0.001
    run_dir = _mujoco_bundle(tmp_path / "run", metrics_endpoint=forged)
    written = _evaluate("mujoco_contact_push_v1", run_dir, tmp_path / "eval")
    assert written["status"] == "FAIL", written
    assert "HG-M1-7" in written["hard_gate_ids"]
    facts = scoring.mujoco_facts({
        "telemetry.jsonl": [
            json.loads(line) for line in
            (run_dir / "telemetry.jsonl").read_text(encoding="utf-8").splitlines()],
        "metrics.json": json.loads((run_dir / "metrics.json").read_text(
            encoding="utf-8")),
    }, spec.criteria)
    assert facts["metrics_consistent"] is False
    assert facts["recomputed"]["endpoint_distance_m"] != forged
    assert facts["metrics_mismatches"], facts


# --------------------------------------------------------------------------
# AC-T4-4 快照 sha 与 run_manifest 声明不符 → INVALID_ARTIFACT
# --------------------------------------------------------------------------

def test_ac_p0_4_4_snapshot_mismatch(tmp_path: Path):
    run_dir = _mujoco_bundle(tmp_path / "run",
                             manifest={"candidate_artifact_sha256": "a" * 64})
    out_dir = tmp_path / "eval"
    written = _evaluate("mujoco_contact_push_v1", run_dir, out_dir)
    assert written["status"] == "INVALID_ARTIFACT", written
    assert "HG-EVAL-6" in written["hard_gate_ids"]
    snap = json.loads((out_dir / ev.SNAPSHOT_DIR / ev.SNAPSHOT_MANIFEST)
                      .read_text(encoding="utf-8"))
    assert snap["digest_ok"] is False
    assert snap["aggregate_sha256"] != snap["declared_sha256"]
    assert snap["aggregate_sha256"] == written["snapshot_aggregate_sha256"]


# --------------------------------------------------------------------------
# AC-T4-5 评测器文件被改 → BLOCKED_INFRA（副本上改，仓内不动）
# --------------------------------------------------------------------------

def test_ac_p0_4_5_evaluator_selfcheck(tmp_path: Path):
    before = {rel: ts.sha256_file(_REPO / rel) for rel in ev.EVALUATOR_SOURCES}
    source_root = _tampered_source_root(tmp_path)
    assert ts.sha256_file(source_root / ev.EVALUATOR_SOURCES[0]) != \
        before[ev.EVALUATOR_SOURCES[0]]
    run_dir = _mujoco_bundle(tmp_path / "run")
    out_dir = tmp_path / "eval"
    written = _evaluate("mujoco_contact_push_v1", run_dir, out_dir,
                        source_root=source_root)
    assert written["status"] == "BLOCKED_INFRA", written
    assert written["hard_gate_ids"] == ["HG-EVAL-7"]
    assert all(dim["score"] == 0.0 for dim in written["dimensions"].values())
    # 仓内文件一个字节都没动
    assert {rel: ts.sha256_file(_REPO / rel)
            for rel in ev.EVALUATOR_SOURCES} == before


# --------------------------------------------------------------------------
# AC-T4-6 七态枚举完整；未知状态被 schema 拒绝
# --------------------------------------------------------------------------

def test_ac_p0_4_6_status_enum(tmp_path: Path):
    assert rs.STATUSES == ("BLOCKED_INFRA", "INVALID_RUNTIME",
                           "INCOMPLETE_EVIDENCE", "INVALID_ARTIFACT", "FAIL",
                           "PASS", "NOT_APPLICABLE")
    schema = json.loads(rs.SCHEMA_PATH.read_text(encoding="utf-8"))
    assert set(schema["properties"]["status"]["enum"]) == set(rs.STATUSES)
    dims = {"d": rs.Dimension(weight=1.0, score=1.0, detail="ok")}
    for status in rs.STATUSES:
        result = rs.EvaluationResult(
            task_id="mujoco_contact_push_v1", run_id="enum-check",
            evaluator_id=ev.EVALUATOR_ID,
            evaluator_version=ev.EVALUATOR_VERSION,
            criteria_lock_sha256=_spec("mujoco_contact_push_v1")
            .criteria_lock_sha256,
            status=status, dimensions=dims)
        assert rs.validate_result(result.to_json())["status"] == status
    with pytest.raises(rs.EvaluationError):
        rs.EvaluationResult(
            task_id="mujoco_contact_push_v1", run_id="enum-check",
            evaluator_id=ev.EVALUATOR_ID,
            evaluator_version=ev.EVALUATOR_VERSION,
            criteria_lock_sha256=_spec("mujoco_contact_push_v1")
            .criteria_lock_sha256,
            status="OK", dimensions=dims)
    bad = rs.EvaluationResult(
        task_id="mujoco_contact_push_v1", run_id="enum-check",
        evaluator_id=ev.EVALUATOR_ID, evaluator_version=ev.EVALUATOR_VERSION,
        criteria_lock_sha256=_spec("mujoco_contact_push_v1").criteria_lock_sha256,
        status="PASS", dimensions=dims).to_json()
    bad["status"] = "OK"
    with pytest.raises(rs.EvaluationError):
        rs.validate_result(bad)


# --------------------------------------------------------------------------
# AC-T4-7 权重/阈值来自 criteria.json；无浮点权字面量；阈值带 derivation
# --------------------------------------------------------------------------

def test_ac_p0_4_7_criteria_data(tmp_path: Path):
    source = (_REPO / "orchestrator/benchmark/scoring.py").read_text(
        encoding="utf-8")
    assert scoring.float_literals(source) == []
    # 阈值全带非空 derivation；三份判据 criteria_version/locked_at 齐全
    for task_id in ("mujoco_contact_push_v1", "libero_env_adapter_v1",
                    "icy_ground_dr_v1"):
        spec = _spec(task_id)
        assert isinstance(spec.criteria["criteria_version"], int)
        assert spec.criteria["criteria_version"] >= 1
        assert spec.criteria["locked_at"], task_id
        assert spec.criteria["evaluator_files"], task_id
        for name, entry in spec.thresholds.items():
            assert entry["derivation"].strip(), (task_id, name)
    # 改 json（权重）→ 分数变化；改代码无效（scoring.py 无字面量）
    run_dir = _mujoco_bundle(tmp_path / "run")
    spec = _spec("mujoco_contact_push_v1")
    files = {
        "telemetry.jsonl": [
            json.loads(line) for line in
            (run_dir / "telemetry.jsonl").read_text(encoding="utf-8").splitlines()],
        "metrics.json": json.loads((run_dir / "metrics.json").read_text(
            encoding="utf-8")),
        "reproducibility.json": json.loads(
            (run_dir / "reproducibility.json").read_text(encoding="utf-8")),
    }
    base = scoring.score_mujoco(files, spec.criteria)
    mutated_criteria = json.loads(json.dumps(
        ts._thaw(spec.criteria), ensure_ascii=False))
    first, second = sorted(mutated_criteria["weights"])[:2]
    mutated_criteria["weights"][first], mutated_criteria["weights"][second] = (
        mutated_criteria["weights"][second], mutated_criteria["weights"][first])
    moved = scoring.score_mujoco(files, mutated_criteria)
    assert {name: dim.weight for name, dim in moved.items()} != \
        {name: dim.weight for name, dim in base.items()}
    # 阈值也来自数据：把穿模上限压到实测值以下，physics 分数必降
    stricter = json.loads(json.dumps(ts._thaw(spec.criteria), ensure_ascii=False))
    stricter["thresholds"]["max_penetration_m"]["value"] = 0.0
    assert scoring.score_mujoco(files, stricter)["physics_validity"].score < \
        base["physics_validity"].score


# --------------------------------------------------------------------------
# AC-T4-8 评测器独立进程：sitecustomize 劫持无效
# --------------------------------------------------------------------------

def test_ac_p0_4_8_process_isolation(tmp_path: Path):
    run_dir = _mujoco_bundle(tmp_path / "run")
    hijack = run_dir / "sitecustomize.py"
    hijack.write_text(
        "from pathlib import Path\n"
        "Path('HIJACKED.txt').write_text('pwned', encoding='utf-8')\n",
        encoding="utf-8")
    script = str(_REPO / "scripts/benchmark/evaluate_run.py")
    env = {**os.environ, "PYTHONPATH": str(run_dir)}

    # ① 受污染的启动环境：fail-closed（rc=3），不产出任何评测结果
    dirty_out = tmp_path / "eval-dirty"
    dirty = subprocess.run(
        [sys.executable, script, "--task", "mujoco_contact_push_v1",
         "--run-dir", str(run_dir), "--out", str(dirty_out)],
        cwd=str(run_dir), env=env, capture_output=True, text=True)
    assert dirty.returncode == 3, (dirty.returncode, dirty.stdout, dirty.stderr)
    assert "拒绝在受污染的启动环境里评测" in dirty.stderr, dirty.stderr
    assert not (dirty_out / rs.EVALUATION_NAME).exists()
    (run_dir / "HIJACKED.txt").unlink(missing_ok=True)

    # ② 隔离解释器（-I）：劫持文件根本不会被导入，评测照常 PASS
    clean_out = tmp_path / "eval"
    proc = subprocess.run(
        [sys.executable, "-I", script, "--task", "mujoco_contact_push_v1",
         "--run-dir", str(run_dir), "--out", str(clean_out)],
        cwd=str(run_dir), env=env, capture_output=True, text=True)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "status=PASS" in proc.stdout, proc.stdout
    assert not (run_dir / "HIJACKED.txt").exists()
    assert _load_result(clean_out)["status"] == "PASS"


# --------------------------------------------------------------------------
# 支撑断言
# --------------------------------------------------------------------------

def test_libero_golden_passes(tmp_path: Path):
    run_dir = _libero_bundle(tmp_path / "run")
    written = _evaluate("libero_env_adapter_v1", run_dir, tmp_path / "eval")
    assert written["status"] == "PASS", written
    assert written["hard_gate_ids"] == []


def test_libero_domain_gates_fire(tmp_path: Path):
    cases = {
        "HG-L1-2": dict(probe_python="/usr/bin/python9"),
        "HG-L1-4": dict(b2_status="FAIL"),
        "HG-L1-5": dict(probe_action_dim=8),
        "HG-L1-6": dict(advanced=False),
        "HG-L1-3": dict(adapter_source=(
            "def build_env():\n    source = 'x = 1'\n"
            "    exec(source)\n    return {}\n")),
        "HG-L1-7": dict(adapter_source=(
            "from libero.benchmark import run_demo\n\n"
            "def build_env():\n    return {}\n")),
    }
    for gate, kwargs in cases.items():
        run_dir = _libero_bundle(tmp_path / gate, **kwargs)
        written = _evaluate("libero_env_adapter_v1", run_dir,
                            tmp_path / f"eval-{gate}")
        assert written["status"] == "FAIL", (gate, written["status"])
        assert gate in written["hard_gate_ids"], (gate, written["hard_gate_ids"])


def test_research_golden_passes(tmp_path: Path):
    run_dir = _research_bundle(tmp_path / "run")
    written = _evaluate("icy_ground_dr_v1", run_dir, tmp_path / "eval")
    assert written["status"] == "PASS", written
    # 本任务无物理/复现/隐藏输入：对应三门记 NOT_APPLICABLE 而非沉默
    assert "HG-EVAL-2" in written["not_applicable_gates"]
    assert "HG-EVAL-3" in written["not_applicable_gates"]
    assert "HG-EVAL-4" in written["not_applicable_gates"]


def test_research_domain_gates_fire(tmp_path: Path):
    cases = {
        "HG-R1-1": dict(drop_section="failure_taxonomy"),
        "HG-R1-2": dict(missing_unit=True),
        "HG-R1-3": dict(overlap_holdout=True),
        "HG-R1-4": dict(drop_section="dataset_splits"),
        "HG-R1-5": dict(drop_section="metrics"),
        "HG-R1-6": dict(claimed_success=True, criterion_met=False),
        "HG-R1-7": dict(report=_R1_REPORT + "\n已翻案：冰面实验已成功。\n"),
    }
    for gate, kwargs in cases.items():
        run_dir = _research_bundle(tmp_path / gate, **kwargs)
        written = _evaluate("icy_ground_dr_v1", run_dir,
                            tmp_path / f"eval-{gate}")
        assert written["status"] == "FAIL", (gate, written["status"])
        assert gate in written["hard_gate_ids"], (gate, written["hard_gate_ids"])


def test_gate_log_covers_every_declared_gate(tmp_path: Path):
    """每个 criteria 声明的硬门都必须被检查过（没有「没查就过」的门）。"""
    run_dir = _mujoco_bundle(tmp_path / "run")
    written = _evaluate("mujoco_contact_push_v1", run_dir, tmp_path / "eval")
    declared = {gate["id"] for gate in
                _spec("mujoco_contact_push_v1").hard_gates}
    assert declared <= set(written["gates_checked"])
    for gate in ("HG-EVAL-1", "HG-EVAL-2", "HG-EVAL-3", "HG-EVAL-4",
                 "HG-EVAL-5", "HG-EVAL-6", "HG-EVAL-7"):
        assert gate in written["gates_checked"]


def test_snapshot_is_copy_not_reference(tmp_path: Path):
    """评测后再改候选目录，不影响已落快照（D4-1）。"""
    run_dir = _mujoco_bundle(tmp_path / "run")
    out_dir = tmp_path / "eval"
    written = _evaluate("mujoco_contact_push_v1", run_dir, out_dir)
    snap_metrics = (out_dir / ev.SNAPSHOT_DIR / "metrics.json").read_bytes()
    (run_dir / "metrics.json").write_text("{}\n", encoding="utf-8")
    assert (out_dir / ev.SNAPSHOT_DIR / "metrics.json").read_bytes() == \
        snap_metrics
    assert written["status"] == "PASS"


def test_snapshot_manifest_and_sha_are_consistent(tmp_path: Path):
    run_dir = _mujoco_bundle(tmp_path / "run")
    out_dir = tmp_path / "eval"
    written = _evaluate("mujoco_contact_push_v1", run_dir, out_dir)
    path = out_dir / ev.SNAPSHOT_DIR / ev.SNAPSHOT_MANIFEST
    assert written["snapshot_manifest_sha256"] == ts.sha256_file(path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert "run_manifest.json" not in doc["digest_set"]
    assert doc["digest_ok"] is True


# --------------------------------------------------------------------------
# 评审 W3-02：输入件必须按冻结 schema 校验（删段即豁免硬门的漏洞）
# --------------------------------------------------------------------------

def _rewrite_json(path: Path, mutate) -> None:
    doc = json.loads(path.read_text(encoding="utf-8"))
    mutate(doc)
    path.write_bytes(_encode(doc))


def _rebind_manifest(run_dir: Path) -> None:
    """改写指纹后重绑 run_manifest 的摘要，让失败只可能来自 schema 本身。

    不重绑的话 HG-EVAL-6 会因聚合/绑定不符先红，测不出「schema 有没有被校验」。
    """
    path = run_dir / "run_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["runtime_fingerprint_sha256"] = hashlib.sha256(
        (run_dir / "runtime_fingerprint.json").read_bytes()).hexdigest()
    path.write_bytes(_encode(manifest))


def _gate_detail(out_dir: Path, gate: str) -> str:
    doc = _load_result(out_dir)
    hits = [item["detail"] for item in doc["hard_gates"] if item["id"] == gate]
    assert hits, doc["hard_gates"]
    return hits[0]


def test_w3_02_input_schema_violations_are_invalid_artifact(tmp_path: Path):
    """删除必填段 / 类型不对 / 缺 paid_model_calls 一律 HG-EVAL-6。"""
    # (a) runtime_fingerprint 缺 simulator 段（M1 必须有模拟器身份）
    run_a = _mujoco_bundle(tmp_path / "a")
    _rewrite_json(run_a / "runtime_fingerprint.json",
                  lambda doc: doc.pop("simulator"))
    _rebind_manifest(run_a)
    out_a = tmp_path / "a_eval"
    written_a = _evaluate("mujoco_contact_push_v1", run_a, out_a)
    assert written_a["status"] == "INVALID_ARTIFACT", written_a
    assert "HG-EVAL-6" in written_a["hard_gate_ids"], written_a
    assert "不合 runtime_fingerprint.v1.schema.json" in _gate_detail(
        out_a, "HG-EVAL-6")

    # (b) action_contract.dim 写成字符串（schema 要求 integer）
    run_b = _mujoco_bundle(tmp_path / "b")
    _rewrite_json(
        run_b / "runtime_fingerprint.json",
        lambda doc: doc["simulator"]["action_contract"].__setitem__("dim", "7"))
    _rebind_manifest(run_b)
    out_b = tmp_path / "b_eval"
    written_b = _evaluate("mujoco_contact_push_v1", run_b, out_b)
    assert written_b["status"] == "INVALID_ARTIFACT", written_b
    assert "不合 runtime_fingerprint.v1.schema.json" in _gate_detail(
        out_b, "HG-EVAL-6")

    # (c) run_manifest 省略 paid_model_calls（「0 付费」不得靠省略字段豁免）
    run_c = _mujoco_bundle(tmp_path / "c")
    _rewrite_json(run_c / "run_manifest.json",
                  lambda doc: doc.pop("paid_model_calls"))
    out_c = tmp_path / "c_eval"
    written_c = _evaluate("mujoco_contact_push_v1", run_c, out_c)
    assert written_c["status"] == "INVALID_ARTIFACT", written_c
    detail_c = _gate_detail(out_c, "HG-EVAL-6")
    assert "paid_model_calls" in detail_c, detail_c


# --------------------------------------------------------------------------
# 评审 W3-01：私有根路径按分段归一化判定（拼写变体不得溜过）
# --------------------------------------------------------------------------

def test_w3_01_hidden_path_normalization(tmp_path: Path):
    traversal = [
        "./private/hidden/x.json",
        "x/../../private/hidden/y.json",
        "/mnt/artifacts/simpraxis/benchmark/roadmap_phase0/private/hidden/z.json",
        "private\\hidden\\w.json",
    ]
    for raw in traversal:
        assert ev.private_hit(raw), raw
    for benign in ("private/other/x.json", "hidden/x.json", "privatex/hidden/x"):
        assert not ev.private_hit(benign), benign

    run_dir = _mujoco_bundle(tmp_path / "run", hidden_reads=traversal)
    out_dir = tmp_path / "eval"
    written = _evaluate("mujoco_contact_push_v1", run_dir, out_dir)
    assert written["status"] == "FAIL", written
    assert "HG-EVAL-2" in written["hard_gate_ids"], written
    detail = _gate_detail(out_dir, "HG-EVAL-2")
    for needle in ("x.json", "y.json", "z.json", "w.json"):
        assert needle in detail, (needle, detail)

    # 同一份产物、只把审计件换成非私有路径 → 不误伤，仍 PASS
    clean = _mujoco_bundle(tmp_path / "clean",
                           hidden_reads=["private/other/x.json",
                                         "hidden/x.json"])
    assert _evaluate("mujoco_contact_push_v1", clean,
                     tmp_path / "clean_eval")["status"] == "PASS"


# --------------------------------------------------------------------------
# 评审 W3-08：禁语判定必须识别否定语境（收窄误杀，不放宽判据）
# --------------------------------------------------------------------------

def test_w3_08_negation_aware_forbidden_claims():
    # 明确否定 → 不算肯定式违规
    assert scoring.affirmative_claims("本方案不声称零样本成功") == []
    assert scoring.affirmative_claims("不能仅扩大分布就宣称鲁棒") == []
    assert scoring.affirmative_claims("我们没有实现已翻案") == []
    # 肯定句照旧命中（不是把判据放水）
    assert scoring.affirmative_claims("我们实现了零样本成功") == ["零样本成功"]
    assert scoring.affirmative_claims("本工作已翻案") == ["已翻案"]
    # 否定标记只在同一分句、且位于禁语之前才生效
    assert scoring.affirmative_claims("不。已翻案") == ["已翻案"]
    assert scoring.affirmative_claims("这不是空话，已经零样本成功") == [
        "零样本成功"]


def test_evaluator_files_format_roundtrip():
    entries = ev.format_evaluator_files()
    assert len(entries) == len(ev.EVALUATOR_SOURCES)
    parsed = ev.parse_evaluator_files(entries)
    assert set(parsed) == set(ev.EVALUATOR_SOURCES)
    assert ev.self_check(_spec("mujoco_contact_push_v1")) == []
    with pytest.raises(ev.EvaluatorError):
        ev.parse_evaluator_files(["no-colon-here"])


def test_w3_04_external_anchor_read_side_fails_closed(tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch):
    """评审 W3-04：仓外锚点**只读侧**在场时必须逐件认账；无锚点如实降级。

    修复前的缺陷：``write_evaluator_anchor`` 零生产调用方 ⇒ 锚点永远不落盘，
    docstring 却声称「候选不可写私有根可防仓内双改」——是假防护。修复后写侧
    删除、docstring 如实降级，本用例钉住剩下的那一半：只要有人布了锚（仓外
    私有根），任何源码与锚不符都必须变成问题进行阻断，而不是被忽略。
    """
    root = tmp_path / "bench_root"
    monkeypatch.setenv("SIMPRAXIS_BENCHMARK_ROOT", str(root))
    anchor = ev.evaluator_anchor_path()
    assert anchor is not None and anchor.is_file() is False
    # 无锚点：如实降级（不假报问题，也不声称已防住）
    assert ev._anchor_problems() == []

    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_text(json.dumps({
        "schema": "simpraxis.benchmark_evaluator_anchor.v1",
        "files": {rel: _ZERO_SHA for rel in ev.EVALUATOR_SOURCES},
    }), encoding="utf-8")
    problems = ev._anchor_problems()
    assert len(problems) == len(ev.EVALUATOR_SOURCES), problems
    assert all("与仓外锚点不符" in item for item in problems), problems

    # 锚点缺件同样阻断（不得因「登记不全」静默放行）
    anchor.write_text(json.dumps({"schema": "x", "files": {
        ev.EVALUATOR_SOURCES[0]: _ZERO_SHA}}), encoding="utf-8")
    missing = ev._anchor_problems()
    assert any("未在仓外锚点登记" in item for item in missing), missing

    # 写侧确实不存在（防止「重新加回来但又没人调用」的假防护复发）
    assert not hasattr(ev, "write_evaluator_anchor")


# --------------------------------------------------------------------------
# 评审 W3-11：telemetry 的 step 基序无关（判据从未声明 0 基）
# --------------------------------------------------------------------------

def test_w3_11_step_base_is_agnostic():
    """评审 W3-11 回归：``step`` 只要求整数、严格递增、平移连续。

    修复前 ``step_ok = all(step == index)`` 把 0 基**隐式**写死（plan §3.2 的
    字段清单与 task.yaml 都没声明基序），一份从 1 开始或带偏移的合法
    telemetry 会掉 ``evidence_completeness`` 并触发 HG-EVAL-8 误杀。
    """
    spec = _spec("mujoco_contact_push_v1")
    files = _mujoco_files()
    telemetry = [json.loads(line) for line in
                 files["telemetry.jsonl"].splitlines() if line]
    baseline = scoring.mujoco_facts({"telemetry.jsonl": telemetry}, spec.criteria)
    assert baseline["step_index_monotonic"] is True, baseline["step_index_monotonic"]

    for offset in (1, 7, 1000):
        shifted = [dict(row, step=row["step"] + offset) for row in telemetry]
        facts = scoring.mujoco_facts({"telemetry.jsonl": shifted}, spec.criteria)
        assert facts["step_index_monotonic"] is True, (offset, facts)
        assert facts["steps"] == baseline["steps"]

    # 不是「随便递增就过」：跳号/重复/非整数/空 一律 False（fail-closed）
    assert scoring._step_index_ok([{"step": 0}, {"step": 2}]) is False
    assert scoring._step_index_ok([{"step": 5}, {"step": 5}]) is False
    assert scoring._step_index_ok([{"step": 0}, {"step": 1.0}]) is False
    assert scoring._step_index_ok([{"step": True}, {"step": 2}]) is False
    assert scoring._step_index_ok([]) is False


# --------------------------------------------------------------------------
# 评审 W3-14：浮点字面量扫描必须盖住 BinOp（10 ** -6 一类）
# --------------------------------------------------------------------------

def test_w3_14_binop_float_expressions_are_scanned():
    """评审 W3-14 回归：``10 ** -6`` 这类算术表达式不得逃过 AST 扫描。

    修复前 ``float_literals`` 只收 ``ast.Constant(float)``；冻结的容差
    ``metrics_abs_tol`` 已移入 criteria（判据数据不进代码），扫描本身的盲区
    也要补上，否则未来用同样写法藏阈值不会被 AC-T4-7 抓到。
    """
    sample = "\n".join([
        "A = 10 ** -6",            # 幂：浮点结果，必须被收
        "B = 1e-6",                # 直接浮点常量
        "C = 2 / 3",               # 除法：浮点结果，必须被收
        "D = 3 + 4",               # 整数运算：不是浮点，不得误报
        "E = '10 ** -6'",          # 字符串里的文字不是字面量
    ])
    found = scoring.float_literals(sample)
    assert any(abs(value - 1e-6) < 1e-15 for value in found), found
    assert any(abs(value - (2 / 3)) < 1e-15 for value in found), found
    assert all(not float(value).is_integer() for value in found), found
    # 生产文件本身仍然 0 命中（容差已进 criteria）
    assert scoring.float_literals(
        (_REPO / "orchestrator/benchmark/scoring.py").read_text(
            encoding="utf-8")) == []


# --------------------------------------------------------------------------
# 评审 CR-06：审计件畸形不得 fail-open
# --------------------------------------------------------------------------

def test_f5_cr06_malformed_hidden_audit_is_a_finding(tmp_path: Path):
    """审计件「在场但形状畸形」→ HG-EVAL-2 判负；只有真缺件才是不适用。

    缺陷形态：``{"read": "private/hidden/..."}``（字符串而非列表）或顶层
    ``[]``。文件在场所以 HG-EVAL-5 不触发，旧实现让 HG-EVAL-2 记
    ``applicable=False``——一次真读了隐藏种子的运行被判 PASS。
    """
    cases = {
        "top_level_list": [],
        "string_read": {"read": "private/hidden/mujoco_seeds.json",
                        "written": [], "modified": []},
        "null_read": {"read": None, "written": [], "modified": []},
    }
    for label, payload in cases.items():
        run_dir = _mujoco_bundle(tmp_path / label)
        (run_dir / "hidden_access_audit.json").write_text(
            json.dumps(payload), encoding="utf-8")
        written = _evaluate("mujoco_contact_push_v1", run_dir,
                            tmp_path / f"eval-{label}")
        assert written["status"] == "FAIL", (label, written["status"])
        assert "HG-EVAL-2" in written["hard_gate_ids"], (label, written)
        detail = _gate_detail(tmp_path / f"eval-{label}", "HG-EVAL-2")
        assert "审计件" in detail, (label, detail)
        # 同一畸形件必须在 M1 域门同口径判负（HG-M1-6 与 HG-EVAL-2 同源）
        assert "HG-M1-6" in written["hard_gate_ids"], (label, written)



# --------------------------------------------------------------------------
# 评审 CR-32：metrics.json 缺全部声明键不得空过一致性门
# --------------------------------------------------------------------------

def test_f5_cr32_metrics_missing_declared_keys_fire_consistency(tmp_path: Path):
    """``metrics.json = {}`` ⇒ ``metrics_consistent=False`` 且 HG-M1-7 触发。

    早先「缺键不判伪造，只对已有键对账」，于是什么键都不写时 ``checked == 0``
    → ``consistent=None`` → HG-M1-7 not-applicable，整门从未执行。
    """
    spec = _spec("mujoco_contact_push_v1")
    run_dir = _mujoco_bundle(tmp_path / "run")
    telemetry = [json.loads(line) for line in
                 (run_dir / "telemetry.jsonl").read_text(
                     encoding="utf-8").splitlines() if line.strip()]
    facts = scoring.mujoco_facts({"telemetry.jsonl": telemetry,
                                  "metrics.json": {}}, spec.criteria)
    assert facts["metrics_consistent"] is False
    assert facts["metrics_mismatches"], facts

    empty = _mujoco_bundle(tmp_path / "empty", metrics_override={})
    written = _evaluate("mujoco_contact_push_v1", empty, tmp_path / "eval")
    assert written["status"] == "FAIL", written
    assert "HG-M1-7" in written["hard_gate_ids"], written



# --------------------------------------------------------------------------
# 评审 CR-60：split 内重复 id 判设计失败
# --------------------------------------------------------------------------

def test_f5_cr60_duplicate_ids_within_split_fail_design(tmp_path: Path):
    """``["e1", "e1"]`` 不得塌成一个 id 后空过 HG-R1-3/HG-R1-4。

    声明 2 个 holdout episode 却只有 1 个唯一 id：既不是独立留出，也不是
    无重复的三段切分。列表长度 ≠ 集合长度即设计失败。
    """
    spec = _spec("icy_ground_dr_v1")
    files = _research_files(duplicate_holdout=True)
    facts = scoring.research_facts(files, spec.criteria)
    assert facts["split_duplicates"] == {"holdout": ["h1"]}
    assert facts["holdout_ok"] is False
    assert facts["leak_ok"] is False

    run_dir = _research_bundle(tmp_path / "run", duplicate_holdout=True)
    written = _evaluate("icy_ground_dr_v1", run_dir, tmp_path / "eval")
    assert written["status"] == "FAIL", written
    assert "HG-R1-3" in written["hard_gate_ids"], written
    assert "HG-R1-4" in written["hard_gate_ids"], written
