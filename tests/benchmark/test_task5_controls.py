"""Phase 2.8.0 Task 5 机判：AC-T5-1..6（Golden / Negative / Mutation）。

纪律（硬规则 7 / T5.1）：

- **不 mock 评分逻辑**：负例与 golden 都是真实写盘的静态夹具，评测器真读快照；
  唯一被替换的是 AC-T5-6 的「放水评测器」注入点（验证 rc fail-closed 用）；
- **判负也算通过**（D5-5）：断言 ``rejected`` 结构化字段，不用 assert 崩；
- **不碰仓内 golden**：所有变异/删除都在 ``tmp_path`` 副本上做（D5-3）；
- 期望值只在 ``cases.json`` 里声明一次，测试与运行器共用同一份索引。
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import jsonschema
import pytest

from orchestrator.benchmark import controls
from orchestrator.benchmark import evaluator as ev
from orchestrator.benchmark import mutation as mut
from orchestrator.benchmark import result_schema as rs
from orchestrator.benchmark import task_spec as ts

_REPO = Path(__file__).resolve().parents[2]
_FIXTURES = _REPO / "tests/fixtures/benchmark"
_SCHEMAS = ts.BENCHMARK_ROOT / "schemas"
_GOLDEN = _FIXTURES / "golden"
_NEGATIVE = _FIXTURES / "negative"
_SCRIPT = _REPO / "scripts/benchmark/run_negative_controls.py"

#: schema 锁死 additionalProperties=false 的件，_provenance 只能写在
#: 目录级 _provenance.json 里（见各 golden 目录的该文件）
_NO_INLINE_PROVENANCE = {
    "run_manifest.json", "runtime_fingerprint.json", "telemetry.jsonl",
    "_provenance.json",
}


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def controls_report(tmp_path_factory):
    out = tmp_path_factory.mktemp("task5_controls")
    return controls.run_controls(out_root=out)


# --------------------------------------------------------------------------
# AC-T5-1 三套合成 golden 全部 PASS
# --------------------------------------------------------------------------

def test_ac_p0_5_1_golden_pass(tmp_path: Path):
    cases = controls.load_cases(controls.GOLDEN_CASES)
    assert {case["task_id"] for case in cases} == {
        "mujoco_contact_push_v1", "libero_env_adapter_v1", "icy_ground_dr_v1"}
    for case in cases:
        run_dir = _GOLDEN / case["dir"]
        doc = controls.evaluate_case(case["task_id"], run_dir,
                                     tmp_path / case["case_id"])
        assert doc["status"] == "PASS", (case["case_id"], doc)
        assert doc["hard_gates"] == [], (case["case_id"], doc["hard_gates"])
        assert doc["weighted_score"] == pytest.approx(1.0)
        for name, dim in doc["dimensions"].items():
            assert dim["score"] == pytest.approx(1.0), (case["case_id"], name)


def _tracked_under(directory: Path) -> "list[Path]":
    """只检查**入仓的**夹具件：运行时解释器会在夹具目录里写 `__pycache__/*.pyc`
    （`.gitignore` 忽略，非夹具内容），拿目录遍历会把它们当文本读而报编码错。"""
    rel = directory.relative_to(_REPO).as_posix()
    out = subprocess.run(["git", "ls-files", "-z", "--", rel], cwd=_REPO,
                         check=True, capture_output=True)
    return sorted(_REPO / item for item in
                  out.stdout.decode("utf-8").split("\0") if item)


def test_golden_fixtures_are_schema_valid_and_provenanced():
    """T5.5 步骤 1：结构合法 + 逐件 `_provenance` 注记 synthetic_fixture。"""
    fp_schema = _load(_SCHEMAS / "runtime_fingerprint.v1.schema.json")
    manifest_schema = _load(_SCHEMAS / "run_manifest.v1.schema.json")
    for case in controls.load_cases(controls.GOLDEN_CASES):
        run_dir = _GOLDEN / case["dir"]
        jsonschema.validate(_load(run_dir / "run_manifest.json"),
                            manifest_schema)
        jsonschema.validate(_load(run_dir / "runtime_fingerprint.json"),
                            fp_schema)
        directory_prov = _load(run_dir / "_provenance.json")
        assert directory_prov["synthetic_fixture"] is True
        assert directory_prov["case_id"] == case["case_id"]
        assert directory_prov["inline_provenance_exceptions"]
        tracked = _tracked_under(run_dir)
        assert len(tracked) >= 5, case["case_id"]
        for path in tracked:
            if path.name in _NO_INLINE_PROVENANCE:
                continue
            rel = path.relative_to(run_dir).as_posix()
            if path.suffix in (".json", ".yaml"):
                doc = _load(path) if path.suffix == ".json" else __import__(
                    "yaml").safe_load(path.read_text(encoding="utf-8"))
                prov = doc.get("_provenance") if isinstance(doc, dict) else None
                assert isinstance(prov, dict), rel
                assert prov["synthetic_fixture"] is True, rel
                assert prov["case_id"] == case["case_id"], rel
            else:                                  # .md / .py / 其它文本
                text = path.read_text(encoding="utf-8")
                assert "_provenance" in text, rel
                assert f"case_id={case['case_id']}" in text, rel


# --------------------------------------------------------------------------
# AC-T5-2 20 条负例（6+7+7）全部 rejected，且 reason 命中预期
# --------------------------------------------------------------------------

_CASE_COUNTS = {"mujoco_contact_push_v1": 6, "libero_env_adapter_v1": 7,
                "icy_ground_dr_v1": 7}


def test_ac_p0_5_2_negative_controls(controls_report):
    assert controls_report.golden["all_passed"] is True
    report = controls_report.negatives
    assert report["count"] == sum(_CASE_COUNTS.values()) == 20
    counted = {}
    for row in report["rows"]:
        counted[row["task_id"]] = counted.get(row["task_id"], 0) + 1
        assert row["rejected"] is True, row
        assert row["actual_status"] != "PASS", row
        assert row["gates_ok"] and row["detail_ok"], row
        assert row["expected_gates"], row
        assert row["reason"], row
    assert counted == _CASE_COUNTS, counted
    assert report["rejected"] == 20
    assert report["all_passed"] is True


# --------------------------------------------------------------------------
# AC-T5-3 十类 mutation 全部被拒；逐类断言拒绝状态
# --------------------------------------------------------------------------

def test_ac_p0_5_3_mutations(controls_report):
    expected = {
        "mutate_metrics": ("FAIL", {"HG-M1-7"}),
        "mutate_evidence_manifest": ("INVALID_ARTIFACT", {"HG-EVAL-6"}),
        "mutate_run_manifest": ("INVALID_ARTIFACT", {"HG-EVAL-6"}),
        "replace_fingerprint": ("INVALID_RUNTIME", {"HG-EVAL-1"}),
        "delete_logs": ("INCOMPLETE_EVIDENCE", {"HG-EVAL-5"}),
        "bump_task_version": ("INVALID_ARTIFACT", {"HG-EVAL-6"}),
        "bump_evaluator_version": ("INVALID_ARTIFACT", {"HG-EVAL-6"}),
        "change_threshold": ("BLOCKED_INFRA", {"HG-EVAL-7"}),
        "inject_judge_report": ("INVALID_ARTIFACT", {"HG-EVAL-6"}),
        "forge_pass_file": ("INVALID_ARTIFACT", {"HG-EVAL-6"}),
    }
    assert set(expected) == set(mut.MUTATION_NAMES)
    assert len(expected) == 10
    report = controls_report.mutations
    assert report["count"] == 10
    seen = set()
    for row in report["rows"]:
        name = row["mutation"]
        seen.add(name)
        status, gates = expected[name]
        assert row["actual_status"] == status, row
        assert gates <= set(row["actual_gates"]), row
        assert row["rejected"] is True, row
    assert seen == set(expected)
    assert report["all_passed"] is True


# --------------------------------------------------------------------------
# AC-T5-4 缺失证据不沉默通过：删 telemetry → INCOMPLETE_EVIDENCE
# --------------------------------------------------------------------------

def test_ac_p0_5_4_missing_evidence(tmp_path: Path):
    case = controls.load_cases(controls.GOLDEN_CASES)[0]
    assert case["task_id"] == "mujoco_contact_push_v1"
    copy = tmp_path / "run"
    shutil.copytree(_GOLDEN / case["dir"], copy)
    (copy / "telemetry.jsonl").unlink()
    doc = controls.evaluate_case(case["task_id"], copy, tmp_path / "eval")
    assert doc["status"] == "INCOMPLETE_EVIDENCE", doc
    hit_ids = {hit["id"] for hit in doc["hard_gates"]}
    assert "HG-EVAL-5" in hit_ids, hit_ids
    details = {hit["id"]: hit["detail"] for hit in doc["hard_gates"]}
    assert "telemetry.jsonl" in details["HG-EVAL-5"]


# --------------------------------------------------------------------------
# AC-T5-5 negative_control_results.json schema 正确、计数与夹具数量相符
# --------------------------------------------------------------------------

def _assert_rows(rows, keys, *, rejected=True):
    for row in rows:
        for key in keys:
            assert key in row, (key, row)
        if rejected:
            assert isinstance(row["rejected"], bool), row


def test_ac_p0_5_5_results_json(controls_report):
    out = controls_report.out_root
    golden = _load(out / controls.GOLDEN_RESULTS)
    negative = _load(out / controls.NEGATIVE_RESULTS)
    mutations = _load(out / controls.MUTATION_RESULTS)

    assert golden["schema"] == controls.SCHEMA_GOLDEN
    assert negative["schema"] == controls.SCHEMA_NEGATIVE
    assert mutations["schema"] == controls.SCHEMA_MUTATION
    for doc in (golden, negative, mutations):
        assert doc["generated_at"].endswith("Z")
        assert doc["all_passed"] is True
        assert doc["count"] == len(doc["rows"])

    assert golden["count"] == len(controls.load_cases(controls.GOLDEN_CASES)) == 3
    assert negative["count"] == len(
        controls.load_cases(controls.NEGATIVE_CASES)) == 20
    assert negative["rejected"] == 20
    assert mutations["count"] == len(mut.MUTATIONS) == 10
    assert mutations["rejected"] == 10

    _assert_rows(golden["rows"], ("case_id", "task_id", "actual_status",
                                  "passed"), rejected=False)
    for row in golden["rows"]:
        assert row["passed"] is True
    _assert_rows(negative["rows"], ("case_id", "task_id", "dir", "defect",
                                    "expected_status", "expected_gates",
                                    "actual_status", "actual_gates",
                                    "rejected", "reason"))
    _assert_rows(mutations["rows"], ("case_id", "task_id", "mutation",
                                     "defect", "expected_status",
                                     "expected_gates", "actual_status",
                                     "actual_gates", "rejected", "reason"))
    # 结果 JSON 与 cases.json 一一对应（没有夹具外的幽灵条目）
    index = {case["case_id"] for case in
             controls.load_cases(controls.NEGATIVE_CASES)}
    assert {row["case_id"] for row in negative["rows"]} == index
    assert {row["mutation"] for row in mutations["rows"]} == set(
        mut.MUTATION_NAMES)


# --------------------------------------------------------------------------
# AC-T5-6 任一负例「判过」时 rc≠0（注入放水评测器验证 fail-closed）
# --------------------------------------------------------------------------

class _LenientEvaluator:
    """故意放水的评测器：不看产物，一律写 PASS（用于验证运行器 fail-closed）。"""

    def __init__(self, spec) -> None:
        self.spec = spec

    def evaluate(self, run_dir, out_dir, **_kwargs) -> dict:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        doc = {"task_id": self.spec.task_id, "run_id": Path(run_dir).name,
               "status": "PASS", "weighted_score": 1.0, "hard_gates": [],
               "dimensions": {}}
        (out / rs.EVALUATION_NAME).write_text(
            json.dumps(doc, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8")
        return doc


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "simpraxis_run_negative_controls", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_ac_p0_5_6_fail_closed(tmp_path: Path, monkeypatch):
    script = _load_script_module()

    # ① 正常路径：真评测器 → 全部按预期判负 → rc=0
    assert script._main(["--all", "--out-root", str(tmp_path / "clean")]) == 0

    # ② 注入放水评测器：负例被放过 → all_passed=False 且 rc≠0
    monkeypatch.setattr(controls, "_make_evaluator",
                        lambda spec: _LenientEvaluator(spec))
    report = controls.run_controls(out_root=tmp_path / "lenient")
    assert report.golden["all_passed"] is True     # 放水时 golden 当然「过」
    assert report.negatives["all_passed"] is False
    assert report.mutations["all_passed"] is False
    assert report.all_passed is False
    assert script._main(["--all", "--out-root", str(tmp_path / "lenient2")]) == 1


# --------------------------------------------------------------------------
# 支撑断言：变异只在副本上做（D5-3），golden 原件逐字节不变
# --------------------------------------------------------------------------

def test_mutations_do_not_touch_golden(tmp_path: Path):
    golden_dir = _GOLDEN / "mujoco"
    before = {path.relative_to(golden_dir).as_posix():
              ts.sha256_file(path) for path in golden_dir.rglob("*")
              if path.is_file()}
    for name in mut.MUTATION_NAMES:
        mutated = mut.apply(name, golden_dir, tmp_path / name,
                            benchmark_root=ts.BENCHMARK_ROOT)
        assert mutated.run_dir.is_dir()
        if name == "change_threshold":
            assert mutated.benchmark_root is not None
            assert (mutated.benchmark_root / "criteria").is_dir()
    after = {path.relative_to(golden_dir).as_posix():
             ts.sha256_file(path) for path in golden_dir.rglob("*")
             if path.is_file()}
    assert after == before


def test_mutations_keep_manifest_internally_consistent(tmp_path: Path):
    """除 mutate_evidence_manifest（故意失配）外，变异副本的 manifest 与快照
    聚合一致——拒绝理由必须来自被注入的缺陷，而不是 digest 过期。"""
    golden_dir = _GOLDEN / "mujoco"
    for name in mut.MUTATION_NAMES:
        if name in ("mutate_evidence_manifest", "change_threshold"):
            continue
        mutated = mut.apply(name, golden_dir, tmp_path / name)
        snap = ev.snapshot(mutated.run_dir, tmp_path / f"{name}_snap",
                           ["solution.py", "scene_spec.json", "metrics.json",
                            "telemetry.jsonl", "run_manifest.json"])
        if (mutated.run_dir / "telemetry.jsonl").is_file():
            assert snap.digest_ok is True, name
        else:
            assert snap.missing, name
