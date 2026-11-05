"""Phase 2.8.0 Task 6 机判：AC-T6-1..8（参考基线执行与证据归档，0 付费）。

五条纪律（T6.1 / 硬规则 7）：

- **不 mock、不造假**：AC 全部读**真实落盘**的参考运行产物
  （``<bench_root>/runs/<run_id>/``），数值阈值一律从冻结判据 ``spec.criteria``
  读——测试里不写任何阈值字面量；
- **不写回**：断言 ``run_manifest`` 的 ``criteria_sha256`` / ``criteria_lock_sha256``
  与冻结值逐字相等，证明参考结果没有被拿来回写判据让它通过；
- **不 skip**：产物缺失即失败并指名缺哪一件（先跑
  ``scripts/benchmark/run_reference.py --task <task_id>``）；
- **真容器**：AC-T6-4 打 ``container`` 标记（裸跑收集期 deselect，
  ``SIMPRAXIS_CONTAINER=1`` 才执行）；
- **参考实现不进仓**：AC-T6-7 用 ``git ls-files`` 实扫，不是白名单自述。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import jsonschema
import pytest
import yaml

from orchestrator.benchmark import paths
from orchestrator.benchmark import reference as ref
from orchestrator.benchmark import registry
from orchestrator.benchmark import scoring
from orchestrator.benchmark import task_spec as ts

_REPO = Path(__file__).resolve().parents[2]
_SCHEMAS = ts.BENCHMARK_ROOT / "schemas"
TASKS = ("mujoco_contact_push_v1", "libero_env_adapter_v1", "icy_ground_dr_v1")
#: 逐字段比对复现性时唯一允许变化的键（D6-7）
_TIMING_KEYS = ("wall_time_seconds",)


def _json(path: "str | Path"):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _spec(task_id: str):
    return registry.BenchmarkRegistry().get(task_id)


def _latest_run(task_id: str) -> Path:
    """被判参考运行（评审 W4-11：读 `closeout/run_identity.json` 钉死值）。"""
    try:
        return paths.pinned_run(None, task_id)
    except paths.BenchmarkPathError as exc:
        raise AssertionError(
            f"{task_id}: 没有可判的参考运行产物——先跑 "
            f"scripts/benchmark/run_reference.py --task {task_id}（{exc}）") from exc


def _telemetry(run: Path) -> "list[dict]":
    text = (run / "telemetry.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _evaluation(run: Path) -> dict:
    return _json(paths.evaluator_results_dir(read_only=True) / run.name
                 / "evaluation_result.json")


# --------------------------------------------------------------------------
# AC-T6-1 三个 run_manifest 存在、schema 正确、0 付费、status PASS
# --------------------------------------------------------------------------

def test_ac_p0_6_1_run_manifests():
    schema = _json(_SCHEMAS / "run_manifest.v1.schema.json")
    for task_id in TASKS:
        run = _latest_run(task_id)
        manifest = _json(run / "run_manifest.json")

        jsonschema.validate(manifest, schema)
        assert manifest["schema"] == ref.RUN_MANIFEST_SCHEMA
        assert manifest["mode"] == "reference", manifest["mode"]
        assert manifest["task_id"] == task_id
        assert manifest["run_id"] == run.name
        assert manifest["paid_model_calls"] == 0
        assert manifest["status"] == "PASS", (
            f"{run.name}: status={manifest['status']}（独立评测器判决）")

        # 身份段与冻结定义逐字对账（结果不得回写判据/规格）
        spec = _spec(task_id)
        assert manifest["task_spec_sha256"] == spec.task_file_sha256
        assert manifest["criteria_sha256"] == spec.criteria_sha256
        assert manifest["criteria_lock_sha256"] == spec.criteria_lock_sha256
        assert manifest["task_version"] == int(spec.version)
        assert re.fullmatch(r"[0-9a-f]{40}", manifest["repository_sha"])

        # 证据件逐件在场（指名的四件身份证据）
        for rel in ("runtime_fingerprint.json", "birth/functional_birth.json",
                    "evidence/manifest.json", "run_manifest.json"):
            assert (run / rel).is_file(), f"{run.name}: 缺 {rel}"

        # 独立评测器的判决与 manifest 同值（不是自报）
        result = _evaluation(run)
        assert result["status"] == manifest["status"] == "PASS"
        assert result["run_id"] == manifest["run_id"]
        assert result["task_id"] == task_id
        assert float(result["weighted_score"]) == 1.0, result["weighted_score"]
        assert result["hard_gates"] == []


# --------------------------------------------------------------------------
# AC-T6-2 M1：真实 mj_step；行数 ≥ 100；阈值取自 Task 1 冻结判据
# --------------------------------------------------------------------------

def test_ac_p0_6_2_mujoco_reference():
    task_id = "mujoco_contact_push_v1"
    run = _latest_run(task_id)
    spec = _spec(task_id)

    telemetry = _telemetry(run)
    scene = _json(run / "scene_spec.json")
    files = {
        "telemetry.jsonl": telemetry,
        "metrics.json": _json(run / "metrics.json"),
        "scene_spec.json": scene,
        "reproducibility.json": _json(run / "reproducibility.json"),
    }
    for rel in ("repro/same_seed/metrics.json", "repro/same_seed/telemetry.jsonl",
                "repro/cross_seed/metrics.json", "repro/cross_seed/telemetry.jsonl"):
        path = run / rel
        assert path.is_file(), f"参考运行缺复现件：{rel}"
        if rel.endswith(".jsonl"):
            files[rel] = [json.loads(line) for line in
                          path.read_text(encoding="utf-8").splitlines() if line]
        else:
            files[rel] = _json(path)

    # 真实物理步进：行数 ≥ 100，且时间轴按 dt 逐行推进（不是一份静态文件）
    assert len(telemetry) >= 100, len(telemetry)
    dt = float(scene["dt"])
    for index, row in enumerate(telemetry):
        assert abs(float(row["t"]) - (index + 1) * dt) <= 1e-6, (
            f"第 {index} 行 t={row['t']} 与 (i+1)*dt 不符")

    facts = scoring.mujoco_facts(files, spec.criteria)
    # 判据来自 Task 1 预注册（只读 spec.criteria；取值口径与评分器同一个访问器）
    steps_min = int(scoring.threshold(spec.criteria, "steps_min"))
    steps_max = int(scoring.threshold(spec.criteria, "steps_max"))
    assert steps_min <= facts["steps"] <= steps_max, facts["steps"]
    assert facts["endpoint_distance_m"] <= float(
        scoring.threshold(spec.criteria, "endpoint_distance_m"))
    assert facts["max_penetration_m"] <= float(
        scoring.threshold(spec.criteria, "max_penetration_m"))
    force_min = float(scoring.threshold(spec.criteria, "contact_force_peak_n_min"))
    force_max = float(scoring.threshold(spec.criteria, "contact_force_peak_n_max"))
    assert force_min <= facts["contact_force_peak_n"] <= force_max

    assert facts["all_finite"] is True
    assert facts["step_index_monotonic"] is True
    assert facts["metrics_consistent"] is True, facts["metrics_mismatches"]
    assert facts["cube_moved"] is True
    assert facts["contact_seen"] is True
    assert telemetry[-1]["cube_pos"][0] > telemetry[0]["cube_pos"][0]

    dims = scoring.score_mujoco(files, spec.criteria)
    assert {name: dim.score for name, dim in dims.items()} == {
        name: 1.0 for name in dims}, {n: d.detail for n, d in dims.items()}


# --------------------------------------------------------------------------
# AC-T6-3 M1：同种子重跑，metrics 逐字段相等（排除 wall_time_seconds）
# --------------------------------------------------------------------------

def test_ac_p0_6_3_mujoco_reproducible():
    run = _latest_run("mujoco_contact_push_v1")
    repro = _json(run / "reproducibility.json")
    assert repro["schema"] == ref.REPRO_SCHEMA
    assert repro["same_seed_identical"] is True, repro["same_seed_diffs"]
    assert repro["same_seed_diffs"] == []

    seed = int(repro["seed"])
    primary = _json(run / "metrics.json")
    rerun = _json(run / "raw" / "repro" / f"seed{seed}" / "metrics.json")

    compared = [key for key in primary if key not in _TIMING_KEYS]
    assert compared, "metrics.json 里没有可比字段"
    for key in compared:
        assert primary[key] == rerun[key], (
            f"同种子重跑 {key} 不等：{primary[key]!r} ≠ {rerun[key]!r}")
    assert ref.compare_metrics_same_seed(primary, rerun)["identical"] is True


# --------------------------------------------------------------------------
# AC-T6-4 L1：真容器里 import/construct/reset/step 全通过（container 标记）
# --------------------------------------------------------------------------

@pytest.mark.container
def test_ac_p0_6_4_libero_reference():
    assert os.environ.get("SIMPRAXIS_CONTAINER") == "1", (
        "container 车道须显式开启（SIMPRAXIS_CONTAINER=1）")
    run = _latest_run("libero_env_adapter_v1")

    birth = _json(run / "birth" / "functional_birth.json")
    assert birth["birth_status"] == "BORN", birth["problems"]
    assert set(birth["levels"]) >= {"B0", "B1", "B2", "B3", "B4"}
    for level, entry in birth["levels"].items():
        if birth["required"].get(level):
            assert entry["status"] == "PASS", (level, entry["reason"])
    for level in ("B1", "B2", "B3", "B4"):
        assert birth["levels"][level]["status"] == "PASS", (
            f"{level} 未通过（L1 的出生契约要求真 import/构造/契约/步进）")

    probe = _json(run / "env_probe.json")
    assert probe["captured_in"] == "container"
    assert probe["observation_keys"], "obs keys 为空"
    assert isinstance(probe["action_dim"], int) and probe["action_dim"] > 0
    assert probe["bddl_file"].endswith(".bddl")

    fingerprint = _json(run / "runtime_fingerprint.json")
    container = fingerprint["container"]
    assert container["backend"] == "docker"
    assert str(container["image_digest"]).startswith("sha256:")
    assert container["container_id"] is None       # 一次性容器：身份由 digest 承担
    packages = fingerprint["python"]["packages"]
    assert packages["robosuite"] == "1.4.0", packages.get("robosuite")
    # 宿主冒充容器的反证：探针解释器 == 指纹解释器，且不是主 venv
    assert probe["python_executable"] == fingerprint["python"]["executable"]
    assert probe["python_executable"] != ref.MAIN_PYTHON

    # task.yaml 未声明数值 action_dim（冻结契约只要求「与实测一致」，见
    # Phase_2.8.0_Plan_Complete 的偏差登记），故机判取三处实测互证：
    # env_probe ↔ 指纹 simulator.action_contract ↔ one_step_result（HG-L1-5 由
    # 独立评测器另判）。
    simulator = fingerprint["simulator"]
    assert simulator["name"] == "libero" and simulator["version"]
    assert int(simulator["action_contract"]["dim"]) == probe["action_dim"]

    step = _json(run / "one_step_result.json")
    assert step["advanced"] is True
    assert int(step["action_dim"]) == probe["action_dim"]
    assert step["observation_keys"] == probe["observation_keys"]
    assert any(float(value) != 0.0 for value in step["state"].values()), (
        "零动作后状态全零：环境没有真的推进")
    assert _evaluation(run)["status"] == "PASS"


# --------------------------------------------------------------------------
# AC-T6-5 主 venv 隔离：pip freeze 前后逐字节相等（锁内适配 2）
# --------------------------------------------------------------------------

def test_ac_p0_6_5_main_venv_untouched():
    run = _latest_run("libero_env_adapter_v1")
    before = _json(run / "raw" / "venv_freeze_before.json")
    after = _json(run / "raw" / "venv_freeze_after.json")

    assert before["python"] == after["python"] == ref.MAIN_PYTHON
    assert before["stdout_sha256"] == after["stdout_sha256"], (
        "L1 执行前后主 venv 的 pip freeze 不等：依赖漏进了主 venv")
    assert before["lines"] == after["lines"]
    assert before["lines"] > 50, before["lines"]

    names = {entry.split("==", 1)[0].strip().lower().replace("_", "-")
             for entry in before["packages"]}
    leaked = sorted(names & {"libero", "robosuite", "bddl"})
    assert leaked == [], f"主 venv 里出现容器专属依赖：{leaked}"
    assert any(name == "mujoco" for name in names), "主 venv 应有 mujoco 3.15.0"


# --------------------------------------------------------------------------
# AC-T6-6 R1：四件产物 + 13 项必填段 + schema；结论保持诚实
# --------------------------------------------------------------------------

def test_ac_p0_6_6_research_reference():
    task_id = "icy_ground_dr_v1"
    run = _latest_run(task_id)
    spec = _spec(task_id)

    for rel in spec.outputs:
        assert (run / rel).is_file(), f"{run.name}: 缺产物 {rel}"

    plan_text = (run / "experiment_plan.yaml").read_text(encoding="utf-8")
    plan = yaml.safe_load(plan_text)
    assert plan["schema"] == "simpraxis.benchmark_experiment_plan.v1"
    missing = [name for name in scoring.R1_REQUIRED_SECTIONS
               if plan.get(name) in (None, "", [], {})]
    assert missing == [], f"13 项必填段缺：{missing}"

    space = _json(run / "parameter_space.json")
    metrics_doc = _json(run / "evaluation_metrics.json")
    assert space["schema"] == "simpraxis.benchmark_parameter_space.v1"
    assert metrics_doc["schema"] == "simpraxis.benchmark_evaluation_metrics.v1"
    # 参考基线不训练、不出数据：结论字段必须是诚实的 false（不是失败）
    conclusion = metrics_doc["conclusion"]
    assert conclusion["criterion_met"] is False
    assert conclusion["claimed_success"] is False
    assert scoring.R1_FORBIDDEN_CLAIMS and not [
        phrase for phrase in scoring.R1_FORBIDDEN_CLAIMS
        if phrase in plan_text + (run / "research_report.md").read_text(
            encoding="utf-8")]

    files = {"experiment_plan.yaml": plan,
             "research_report.md": (run / "research_report.md").read_text(
                 encoding="utf-8"),
             "parameter_space.json": space,
             "evaluation_metrics.json": metrics_doc}
    facts = scoring.research_facts(files, spec.criteria)
    assert facts["missing_sections"] == []
    assert facts["uncovered_classes"] == []
    assert facts["forbidden_claims"] == []
    assert facts["leak_ok"] is True and facts["overlap_ids"] == []
    assert facts["contradiction"] is False

    dims = scoring.score_research(files, spec.criteria)
    assert {name: dim.score for name, dim in dims.items()} == {
        name: 1.0 for name in dims}, {n: d.detail for n, d in dims.items()}
    assert _evaluation(run)["status"] == "PASS"


# --------------------------------------------------------------------------
# AC-T6-7 参考实现不在仓内（git 实扫 + assert_private 双向）
# --------------------------------------------------------------------------

def test_ac_p0_6_7_private_only():
    listed = subprocess.run(["git", "ls-files"], cwd=str(_REPO),
                            capture_output=True, text=True, timeout=300)
    assert listed.returncode == 0, listed.stderr[-800:]
    hits = [line for line in listed.stdout.splitlines()
            if re.search(r"references/.*\.(py|yaml)$", line)]
    assert hits == [], f"参考实现混进仓内：{hits}"

    # 仓内路径一律拒绝（含仓根本身）
    with pytest.raises(ref.ReferenceError):
        ref.assert_private(_REPO / "benchmarks" / "research_engineering_v1"
                           / "solution.py")
    with pytest.raises(ref.ReferenceError):
        ref.assert_private(_REPO)
    # 私有根内的参考实现可解析，且 recipe 逐件 sha256 复核通过
    inside = ref.assert_private(paths.private_dir() / "references")
    assert _REPO.resolve() not in inside.parents
    for task_id in TASKS:
        resolved = ref.resolve_reference(task_id)
        assert resolved.directory.is_dir()
        assert _REPO.resolve() not in resolved.directory.resolve().parents
        assert resolved.entry_path.is_file()
        assert resolved.recipe["schema"] == ref.RECIPE_SCHEMA
        assert resolved.recipe["task_id"] == task_id


# --------------------------------------------------------------------------
# AC-T6-8 挂载白名单：-v 目标 ⊆ {/work,/skills,/assets,/out} 且 :ro ≥ 3
# --------------------------------------------------------------------------

def test_ac_p0_6_8_mount_whitelist():
    run = _latest_run("libero_env_adapter_v1")
    argv = _json(run / "raw" / "execution.json")["command"]

    assert ref.validate_container_argv(argv) == []
    flags = ref.parse_mount_flags(argv)
    destinations = {spec.split(":")[1] for spec, _mode in flags}
    assert destinations <= set(ref.MOUNT_WHITELIST), destinations
    assert sum(1 for _spec, mode in flags if mode == "ro") >= ref.MIN_RO_MOUNTS
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    # 镜像按 digest 调用（不得用可变 tag）
    assert any("@sha256:" in token for token in argv), argv[-6:]

    # 越界挂载必须在起容器**之前**被拒
    off_list = [ref.Mount(Path("/tmp"), "/etc", "ro"),
                ref.Mount(Path("/tmp"), "/work", "ro"),
                ref.Mount(Path("/tmp"), "/skills", "ro"),
                ref.Mount(Path("/tmp"), "/assets", "ro")]
    with pytest.raises(ref.ReferenceError):
        ref.docker_argv("sim-praxis/libero:task6@sha256:" + "0" * 64,
                        off_list, ["python3", "adapter.py"])
    only_two_ro = [ref.Mount(Path("/tmp"), "/work", "ro"),
                   ref.Mount(Path("/tmp"), "/skills", "ro"),
                   ref.Mount(Path("/tmp"), "/out", "rw")]
    with pytest.raises(ref.ReferenceError):
        ref.docker_argv("sim-praxis/libero:task6@sha256:" + "0" * 64,
                        only_two_ro, ["python3", "adapter.py"])


# --------------------------------------------------------------------------
# OI-4 隐藏变体登记（非 AC）：manifest 逐件 sha256 ↔ 活文件 + 与 task.yaml 对账
# --------------------------------------------------------------------------

def test_oi4_hidden_variants_registered():
    """OI-4：隐藏变体/种子「由参考执行与已有证据核验后写入」，并 sha256 登记。

    断言的是**可机检的部分**：文件在场、权限 0700/0600、sha256 与登记逐字节
    相符、登记集合与三个任务的 ``reproducibility.hidden_seed_ref`` 声明一致、
    隐藏种子与公开种子不相交。变体数值是否落在 Task 1 derivation 给出的界内
    属**可人审**部分（用户可抽查），不在本断言里重抄一遍界值。
    """
    hidden = paths.private_dir() / "hidden"
    manifest = _json(hidden / "manifest.json")
    assert manifest["schema"] == "simpraxis.benchmark_hidden_manifest.v1"
    assert oct(hidden.stat().st_mode & 0o777) == "0o700"

    files = manifest["files"]
    for name, digest in files.items():
        path = hidden / name
        assert path.is_file(), f"{name}: 登记了但文件不在"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, (
            f"{name}: sha256 与登记不符（隐藏件被改写）")
        assert oct(path.stat().st_mode & 0o777) == "0o600", name

    # 声明 ↔ 登记：task.yaml 指名的 hidden_seed_ref 必须都已登记（反之不要求：
    # M1 的 mujoco_variant.json 是变体覆写表，由 hidden_seed_ref 之外的机制引用）；
    # 登记件的 task_id 必须都属于本阶段三任务，且恰好覆盖声明了隐藏输入的任务
    refs = {_spec(task_id).reproducibility.get("hidden_seed_ref")
            for task_id in TASKS} - {None}
    assert refs <= set(files), f"声明的隐藏件未登记：{sorted(refs - set(files))}"
    owners = {_json(hidden / name)["task_id"] for name in files}
    assert owners <= set(TASKS), owners
    declared_owners = {task_id for task_id in TASKS
                       if _spec(task_id).reproducibility.get("hidden_seed_ref")}
    assert owners == declared_owners, (owners, declared_owners)

    public = set(_spec("mujoco_contact_push_v1").reproducibility["seeds"])
    secret = set(_json(hidden / "mujoco_seeds.json")["seeds"])
    assert public and secret, (public, secret)
    assert not public & secret, f"隐藏种子与公开种子重叠：{public & secret}"


# --------------------------------------------------------------------------
# 回归守卫（非 AC）：指纹探针必须覆盖任务声明的每一个包
# --------------------------------------------------------------------------

def test_task6_probe_covers_declared_packages():
    """HG-EVAL-1 的前提：``runtime.packages`` 里声明的名字都必须有实测值。

    回归背景：Task 2 的探针只测固定观测集，R1 声明的 PyYAML/jsonschema 不在
    其中，于是 ``compare()`` 把整份声明报成「<未安装>」——那是探针覆盖缺口，
    不是真实的环境不符。

    只在**宿主任务**上做本断言：L1 声明的 robosuite/bddl 只存在于隔离镜像内，
    宿主 venv 里本就没有（这正是「不得用主 venv 冒充」的反面），其覆盖由
    容器内指纹与 AC-T6-4 判。
    """
    from orchestrator.benchmark import runtime_fingerprint as rf

    declared: "set[str]" = set()
    for task_id in TASKS:
        spec = _spec(task_id)
        if spec.runtime.get("image_digest"):
            continue                     # 容器任务：宿主探针测不到是正确事实
        declared |= {str(name) for name in (spec.runtime.get("packages") or {})}
    assert declared, "宿主任务都没声明任何包，这条守卫将失去意义"

    capture = rf.HostBackend(packages=sorted(declared)).capture()
    measured = {name for name, version in capture.probe["packages"].items()
                if version}
    assert declared <= measured, f"探针未覆盖：{sorted(declared - measured)}"
    # 值必须来自真实解释器（不是编造）：主 venv 的 mujoco 版本与封印一致
    assert capture.probe["packages"]["mujoco"] == _spec(
        "mujoco_contact_push_v1").runtime["packages"]["mujoco"]
