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
        "birth_result_sha256": hashlib.sha256(
            _encode(files.get("birth/functional_birth.json", b""))).hexdigest(),
        "evaluator_version": ev.EVALUATOR_VERSION,
        "status": "PASS",
        "paid_model_calls": 0,
        "evidence_manifest_sha256": None,
    }
    doc.update(overrides or {})
    return doc


def _host_fingerprint(spec: ts.TaskSpec, packages: dict) -> dict:
    return {
        "schema": "simpraxis.runtime_fingerprint.v1",
        "repository": {}, "config": {}, "mount": {"entries": []},
        "container": {"kind": "host", "backend": "host",
                      "image_digest": None, "image_id": None,
                      "container_id": None, "container_python": None},
        "python": {"executable": spec.runtime["python"],
                   "version": "3.10.12", "workdir": str(_REPO),
                   "packages": dict(packages)},
        "simulator": {},
    }


def _container_fingerprint(spec: ts.TaskSpec) -> dict:
    return {
        "schema": "simpraxis.runtime_fingerprint.v1",
        "repository": {}, "config": {}, "mount": {"entries": []},
        "container": {"kind": "docker", "backend": "docker",
                      "image_digest": spec.runtime["image_digest"],
                      "image_id": spec.runtime["image_digest"],
                      "container_id": None,
                      "container_python": spec.runtime["python"]},
        "python": {"executable": spec.runtime["python"],
                   "version": "3.10.12", "workdir": "/workspace",
                   "packages": {name: str(value) for name, value
                                in spec.runtime["packages"].items()}},
        "simulator": {},
        "action_contract": {"action_dim": 7},
        "observation_contract": {"keys": ["obs", "state"]},
    }


def _mujoco_files(*, steps: "int | None" = None, endpoint_ratio: float = 0.9,
                  penetration_ratio: float = 0.4, force_ratio: float = 0.3,
                  same_seed: bool = True, cross_delta_ratio: float = 0.2,
                  metrics_endpoint: "float | None" = None,
                  fingerprint_packages: "dict | None" = None,
                  hidden_reads: "list[str] | None" = None,
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
    recommitted = scoring.mujoco_facts({"telemetry.jsonl": telemetry})["recomputed"]
    metrics = dict(recommitted)
    if metrics_endpoint is not None:
        metrics["endpoint_distance_m"] = metrics_endpoint

    files: "dict[str, object]" = {
        "solution.py": "def build_scene():\n    return {}\n",
        "scene_spec.json": {"scene": "push", "block_half_size_m": 0.025},
        "metrics.json": metrics,
        "telemetry.jsonl": [json.dumps(row, sort_keys=True) + "\n"
                            for row in telemetry],
        "runtime_fingerprint.json": _host_fingerprint(
            spec, fingerprint_packages
            or {name: str(value) for name, value
                in spec.runtime["packages"].items()}),
        "reproducibility.json": {
            "same_seed_identical": same_seed,
            "cross_seed_max_delta_m": tol * cross_delta_ratio,
        },
        "hidden_access_audit.json": {"read": list(hidden_reads or []),
                                     "written": [], "modified": []},
    }
    # telemetry.jsonl 以原始文本落盘（逐行 JSON）
    files["telemetry.jsonl"] = "".join(files["telemetry.jsonl"])
    (files["metrics.json"]["steps"]) = int(files["metrics.json"]["steps"])
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
research_problem: 冰面域随机化对推箱策略鲁棒性的影响（仅设计，不执行训练）
parameter_definitions:
  - {name: friction, unit: "1", range: [0.05, 0.95]}
probability_distributions:
  friction: {family: uniform, low: 0.05, high: 0.95}
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
    })
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


def test_evaluator_files_format_roundtrip():
    entries = ev.format_evaluator_files()
    assert len(entries) == len(ev.EVALUATOR_SOURCES)
    parsed = ev.parse_evaluator_files(entries)
    assert set(parsed) == set(ev.EVALUATOR_SOURCES)
    assert ev.self_check(_spec("mujoco_contact_push_v1")) == []
    with pytest.raises(ev.EvaluatorError):
        ev.parse_evaluator_files(["no-colon-here"])
