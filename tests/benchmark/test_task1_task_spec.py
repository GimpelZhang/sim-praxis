"""Phase 2.8.0 Task 1 机判：AC-T1-1..7（TaskSpec 契约 + Registry + 判据 LOCK）。

七条 AC 全部**重算再比对**：判据锁由本文件用 ``hashlib.sha256`` 独立复算一遍
（不信任 ``criteria_lock_sha256()`` 的自述），Registry 快照做两次独立加载的
逐字节比较，确定性不是「看起来一样」。

LOCK 纪律（STOP-H）：``criteria_lock_sha256`` 在本 Task 冻结，此后 Task 4 只
回填 ``evaluator_files``/``locked_at``（白名单之外，不改锁），Task 6 只验算。
下面把三个任务的锁值与聚合锁**逐字钉死**，任何无授权改判据的提交都会在这里变红。
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from orchestrator.benchmark import registry, task_spec as ts

_TASKS = Path(__file__).resolve().parents[2] / "benchmarks" / "research_engineering_v1"
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "benchmark" / "tasks"

#: LOCK 冻结值（Task 1 由 `cli lock-criteria` 生成；STOP-H 之后不得漂移）
#: 2026-11-05 评审 W3-06/W3-14 修复后重新 LOCK（判据新增
#: ``metrics_abs_tol`` 与结构常量 ``ablation_min_groups``，evaluator_files 同步
#: 重登记）。冻结值只随「判据内容真的变了」而变，不允许为过测试反向修改。
_LOCKED = {
    "mujoco_contact_push_v1":
        "a9abf30e7ec2ba7aef31dd8efd0c741107358f3fc96bdffd3771f46bbc96f7d9",
    "libero_env_adapter_v1":
        "fc0f0eb8d9599894f09b026dd16f09d9d913ab48c56e56b3b9afddc8cfabb740",
    "icy_ground_dr_v1":
        "bb8334fcf39370bf056f4dc40c08b259b6f92a53acd4dae41d38a50bf08cad4b",
}
_LOCKED_AGGREGATE = (
    "59e78c131f660eb4aaf9a43e870240fedc06adb2595b7f8df5dfd9734de7c302")

#: M1 的七个物理阈值（Plan 只给推导，数值由 Task 1 生成并冻结）
_M1_THRESHOLDS = (
    "endpoint_distance_m",
    "max_penetration_m",
    "contact_force_peak_n_min",
    "contact_force_peak_n_max",
    "steps_min",
    "steps_max",
    "repro_abs_tol",
    "metrics_abs_tol",
)


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------

def _recompute_lock(criteria_path: Path) -> str:
    """独立复算判据锁：只取白名单五键，自建 canonical JSON，自己 sha256。"""
    doc = json.loads(criteria_path.read_text(encoding="utf-8"))
    subset = {key: doc[key] for key in
              ("criteria_version", "weights", "thresholds", "hard_gates", "derivation")}
    blob = json.dumps(subset, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _copy_root(tmp_path: Path) -> Path:
    dst = tmp_path / "bench"
    shutil.copytree(_TASKS, dst)
    return dst


def _set_scalar(path: Path, key: str, value: str) -> None:
    """逐行替换 ``key: "..."``（保住 YAML 注释；恰好一处）。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    hits = [i for i, line in enumerate(lines)
            if line.strip().startswith(f"{key}:")]
    assert len(hits) == 1, f"{path}: 期望恰好 1 行 `{key}:`，实为 {len(hits)}"
    index = hits[0]
    indent = lines[index][:len(lines[index]) - len(lines[index].lstrip())]
    lines[index] = f'{indent}{key}: "{value}"'
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_criteria(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


# --------------------------------------------------------------------------
# AC-T1-1 三个 task.yaml 全部通过 schema；list_tasks() 恰好 3 个 ID
# --------------------------------------------------------------------------

def test_ac_p0_1_1_load_three():
    reg = registry.BenchmarkRegistry()
    specs = reg.load_all()
    assert reg.list_tasks() == ("icy_ground_dr_v1", "libero_env_adapter_v1",
                               "mujoco_contact_push_v1")
    assert len(specs) == 3
    for tid, spec in specs.items():
        assert spec.task_id == tid
        # 单文件校验通过的另一面：task.yaml 的 sha256 与本体重算一致
        assert spec.task_file_sha256 == ts.sha256_file(
            ts.BENCHMARK_ROOT / spec.task_file)
        # 判据锁与独立复算一致（不信任自述）
        assert spec.criteria_lock_sha256 == _recompute_lock(
            ts.BENCHMARK_ROOT / spec.criteria_file)
        assert spec.evaluation["criteria_lock_sha256"] == spec.criteria_lock_sha256
    assert reg.check_manifest()["ok"] is True, reg.check_manifest()["problems"]


def test_ac_p0_1_1_bad_schema_rejected():
    with pytest.raises(ts.TaskSpecError) as excinfo:
        ts.load_task_spec(_FIXTURES / "bad_schema.yaml",
                          criteria_dir=_FIXTURES / "criteria")
    assert excinfo.value.reason == "schema_violation"


# --------------------------------------------------------------------------
# AC-T1-2 未知字段被拒
# --------------------------------------------------------------------------

def test_ac_p0_1_2_unknown_field():
    with pytest.raises(ts.TaskSpecError) as excinfo:
        ts.load_task_spec(_FIXTURES / "unknown_field.yaml",
                          criteria_dir=_FIXTURES / "criteria")
    assert excinfo.value.reason == "unknown_field"
    assert "unregistered_field" in excinfo.value.detail


# --------------------------------------------------------------------------
# AC-T1-3 缺必要字段被拒（不给默认值兜底）
# --------------------------------------------------------------------------

def test_ac_p0_1_3_missing_field():
    with pytest.raises(ts.TaskSpecError) as excinfo:
        ts.load_task_spec(_FIXTURES / "missing_field.yaml",
                          criteria_dir=_FIXTURES / "criteria")
    assert excinfo.value.reason == "missing_field"
    assert "birth_contract" in excinfo.value.detail


# --------------------------------------------------------------------------
# AC-T1-4 重复 task_id 被拒（不得后者覆盖前者）
# --------------------------------------------------------------------------

def test_ac_p0_1_4_duplicate_id(tmp_path: Path):
    root = _copy_root(tmp_path)
    dup_dir = root / "tasks" / "zz_duplicate"
    dup_dir.mkdir()
    src = root / "tasks" / "mujoco_contact_push" / "task.yaml"
    shutil.copyfile(src, dup_dir / "task.yaml")
    assert (dup_dir / "task.yaml").read_bytes() == src.read_bytes()

    reg = registry.BenchmarkRegistry(root)
    with pytest.raises(registry.RegistryError) as excinfo:
        reg.load_all()
    msg = str(excinfo.value)
    assert "task_id 重复" in msg and "mujoco_contact_push_v1" in msg


# --------------------------------------------------------------------------
# AC-T1-5 锁的敏感面：锁定子集变则两哈希变；只改 evaluator_files 锁不变
# --------------------------------------------------------------------------

def test_ac_p0_1_5_sha_changes(tmp_path: Path):
    # 基线
    base = registry.BenchmarkRegistry(_TASKS)
    base.load_all()
    base_manifest = base.manifest_sha256()
    base_locks = dict(base.criteria_locks())

    # (a) 只改 task.yaml 的非判据字段 → manifest 变、判据锁不变
    root = _copy_root(tmp_path)
    task_yaml = root / "tasks" / "mujoco_contact_push" / "task.yaml"
    text = task_yaml.read_text(encoding="utf-8")
    old_budget = "  wall_time_seconds: 600"
    assert old_budget in text
    task_yaml.write_text(text.replace(old_budget, "  wall_time_seconds: 601", 1),
                         encoding="utf-8")
    reg_a = registry.BenchmarkRegistry(root)
    reg_a.load_all()          # 清单里的 criteria_lock 未变，仍应一致
    assert reg_a.manifest_sha256() != base_manifest
    assert dict(reg_a.criteria_locks()) == base_locks

    # (b) 改判据锁定子集（weights）→ 判据锁变；回填 task.yaml 与 manifest 后可重新加载
    root_b = _copy_root(tmp_path / "b")
    crit_path = root_b / "criteria" / "mujoco_contact_push_v1.criteria.json"
    doc = json.loads(crit_path.read_text(encoding="utf-8"))
    doc["weights"] = {"functional_correctness": 0.5, "physics_validity": 0.2,
                      "reproducibility": 0.2, "evidence_completeness": 0.1}
    _write_criteria(crit_path, doc)
    new_lock = _recompute_lock(crit_path)
    assert new_lock != base_locks["mujoco_contact_push_v1"]

    _set_scalar(root_b / "tasks" / "mujoco_contact_push" / "task.yaml",
                "criteria_lock_sha256", new_lock)
    locks_b = {tid: (new_lock if tid == "mujoco_contact_push_v1" else lock)
               for tid, lock in base_locks.items()}
    _set_scalar(root_b / "manifest.yaml", "criteria_lock",
                ts.criteria_aggregate_sha256(locks_b))
    reg_b = registry.BenchmarkRegistry(root_b)
    reg_b.load_all()
    assert reg_b.criteria_locks()["mujoco_contact_push_v1"] == new_lock
    assert reg_b.manifest_sha256() != base_manifest

    # (c) 只改 evaluator_files（白名单外）→ 判据锁不变，清单仍自洽
    root_c = _copy_root(tmp_path / "c")
    crit_c = root_c / "criteria" / "mujoco_contact_push_v1.criteria.json"
    doc_c = json.loads(crit_c.read_text(encoding="utf-8"))
    doc_c["evaluator_files"] = ["evaluator.py"]
    doc_c["locked_at"] = "2026-11-05T00:00:00Z"
    _write_criteria(crit_c, doc_c)
    reg_c = registry.BenchmarkRegistry(root_c)
    reg_c.load_all()          # verify_manifest=True：清单无需改动
    assert reg_c.criteria_locks() == base_locks
    assert reg_c.manifest_sha256() == base_manifest

    # (d) 改 hard_gates / derivation 同样落在锁定面内
    for key, mutate in (
        ("hard_gates", lambda d: d["hard_gates"].pop()),
        ("derivation", lambda d: d.update(derivation=d["derivation"] + "（篡改）")),
        ("thresholds", lambda d: d["thresholds"].popitem()),
        #: 评审 W1-11：删键当然会改哈希，证明不了「阈值**值**被改会变锁」。
        #: 这里逐值改（0.001 → 0.002），键集不变。
        ("thresholds_value",
         lambda d: d["thresholds"]["max_penetration_m"].__setitem__(
             "value", d["thresholds"]["max_penetration_m"]["value"] * 2)),
    ):
        root_x = _copy_root(tmp_path / key)
        path_x = root_x / "criteria" / "mujoco_contact_push_v1.criteria.json"
        doc_x = json.loads(path_x.read_text(encoding="utf-8"))
        mutate(doc_x)
        _write_criteria(path_x, doc_x)
        assert _recompute_lock(path_x) != base_locks["mujoco_contact_push_v1"], key


# --------------------------------------------------------------------------
# AC-T1-6 相同输入两次加载 → 逐字节相同的确定性 JSON
# --------------------------------------------------------------------------

def test_ac_p0_1_6_deterministic():
    first = registry.BenchmarkRegistry().snapshot_json()
    second = registry.BenchmarkRegistry().snapshot_json()
    assert first == second
    # 同进程内二次调用（命中实例缓存）也必须同字节
    reg = registry.BenchmarkRegistry()
    assert reg.snapshot_json() == reg.snapshot_json()

    # 单文件层面同样确定：两次 load_task_spec 的 to_json 逐字节相同
    path = _TASKS / "tasks" / "mujoco_contact_push" / "task.yaml"
    a = ts.canonical_json(ts.load_task_spec(path).to_json())
    b = ts.canonical_json(ts.load_task_spec(path).to_json())
    assert a == b
    # 与快照里对应条目逐字节一致（快照不是另一条路径拼出来的）
    snapshot = json.loads(first)
    entry = [t for t in snapshot["tasks"]
             if t["task_id"] == "mujoco_contact_push_v1"]
    assert len(entry) == 1
    assert ts.canonical_json(entry[0]) == a


# --------------------------------------------------------------------------
# AC-T1-7 加载期不执行解题代码（无 subprocess / 无 import_module）
# --------------------------------------------------------------------------

def test_ac_p0_1_7_no_execution(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("加载期不得启动子进程或动态 import")

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, _boom)
    monkeypatch.setattr(importlib, "import_module", _boom)

    reg = registry.BenchmarkRegistry()
    specs = reg.load_all()
    assert len(specs) == 3
    assert reg.snapshot_json()
    assert reg.check_manifest()["ok"] is True


def test_ac_p0_1_7_no_dynamic_code_in_source():
    """静态面：两个模块都不 import 执行类模块、不调用 exec/eval/compile。"""
    banned_imports = {"subprocess", "importlib", "runpy", "ctypes"}
    banned_calls = {"exec", "eval", "compile", "__import__"}
    for name in ("task_spec.py", "registry.py", "paths.py"):
        path = Path(ts.__file__).parent / name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in banned_calls, (name, node.func.id)
        assert not (imported & banned_imports), (name, imported & banned_imports)


# --------------------------------------------------------------------------
# 支撑断言：判据内容本身（阈值形状 / 推导非空 / 权重和 / 硬门）
# --------------------------------------------------------------------------

def test_thresholds_are_derived_not_invented():
    specs = registry.BenchmarkRegistry().load_all()
    m1 = specs["mujoco_contact_push_v1"]
    # 断言的是「哪些阈值存在」，不是 JSON 键序（判据文件按 sort_keys 落盘，
    # 键序属序列化细节，钉序会让一次无内容的排序变化变成假红）。
    assert set(m1.thresholds) == set(_M1_THRESHOLDS)
    for key, item in m1.thresholds.items():
        assert isinstance(item["value"], (int, float))
        assert not isinstance(item["value"], bool)
        assert item["unit"].strip(), key
        # 推导必须长到能看出算式来源，不是一句「按经验」
        assert len(item["derivation"].strip()) >= 40, (key, item["derivation"])
    # L1 是纯契约判定：无阈值；R1 只承载 rubric 冻结的**结构常量**
    # （评审 W3-06：Ablation ≥ 3 组从散文落成判据），不是物理阈值。
    assert dict(specs["libero_env_adapter_v1"].thresholds) == {}
    r1_thresholds = dict(specs["icy_ground_dr_v1"].thresholds)
    assert set(r1_thresholds) == {"ablation_min_groups"}
    assert r1_thresholds["ablation_min_groups"]["value"] == 3
    assert r1_thresholds["ablation_min_groups"]["unit"] == "group"

    for tid, spec in specs.items():
        assert abs(sum(float(w) for w in spec.weights.values()) - 1.0) < 1e-9, tid
        assert spec.hard_gates, tid
        for gate in spec.hard_gates:
            assert ts.GATE_ID_RE.match(gate["id"]), gate["id"]
        assert spec.criteria["criteria_version"] >= 1
        assert spec.budget["model_calls"] == 0, f"{tid}: 本阶段 0 付费"
        assert spec.constraints["network"] == "disabled", tid


def test_locks_are_frozen():
    reg = registry.BenchmarkRegistry()
    assert dict(reg.criteria_locks()) == _LOCKED
    assert reg.criteria_aggregate_sha256() == _LOCKED_AGGREGATE
    assert reg.read_manifest()["criteria_lock"] == _LOCKED_AGGREGATE


# --------------------------------------------------------------------------
# 支撑断言：Task 1 证据件在场且与重算一致
# --------------------------------------------------------------------------

@pytest.fixture
def bench(monkeypatch) -> Path:
    monkeypatch.delenv("SIMPRAXIS_PHASE2_ROOT", raising=False)
    from orchestrator.benchmark import paths
    root = paths.bench_root(read_only=True)
    assert root.is_dir(), f"数据根不在场：{root}"
    return root


def test_task1_specs_evidence(bench: Path):
    specs = bench / "specs"
    report = json.loads((specs / "validation_report.json").read_text(encoding="utf-8"))
    snapshot = json.loads((specs / "registry_snapshot.json").read_text(encoding="utf-8"))
    schemas = json.loads((specs / "schemas_sha256.json").read_text(encoding="utf-8"))

    reg = registry.BenchmarkRegistry()
    assert report["ok"] is True, report["problems"]
    assert report["manifest_sha256"] == reg.manifest_sha256()
    assert report["criteria_lock"] == _LOCKED_AGGREGATE
    assert [t["task_id"] for t in report["tasks"]] == list(reg.list_tasks())
    # 快照与当下注册表逐字节同构（证据件不是陈货）
    assert ts.canonical_json(snapshot) == reg.snapshot_json()

    on_disk = {p.name: ts.sha256_file(p)
               for p in sorted((reg.root / "schemas").glob("*.schema.json"))}
    assert schemas["schemas"] == on_disk
    assert len(on_disk) == 4, on_disk
