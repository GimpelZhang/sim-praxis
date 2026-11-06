"""P281-1 候选评测器测试（AC-P281-1-06..15）。

三条主线：

1. **判决与拒收面**（06/07/08/09）：锚不可信、自述与观测不符、缺交付件、出生未过——
   四种情形各走各的判决，不许互相顶替（尤其：出生未过必须 FAIL，不能算基础设施问题）。
2. **parity**（10/11/12）：与 Phase 2.8.0 判据语义逐条相等。这里做的是**真**
   parity——同一份产物分别喂给 Phase 0 ``evaluator.evaluate_run`` 与
   ``CandidateEvaluator``，再逐维比对布尔；不是「两个空函数互相等」。
3. **执行面纪律**（13/14/15）：隔离子进程的 argv/env/cwd、评测器源码里不存在
   任何执行候选代码的入口、结果件回灌 schema 校验器。

parity 的诚实边界（报告里要照抄）：它证明的是「在参考运行与负控输入上判据语义
一致」，**不**证明「在所有可能的候选产物上一致」——后者不可判定。
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.benchmark import scoring
from orchestrator.benchmark.evaluator import evaluate_run
from orchestrator.benchmark.phase281 import anchor as anchor_mod
from orchestrator.benchmark.phase281 import candidate_manifest as cm
from orchestrator.benchmark.phase281 import candidate_evaluator as ce
from orchestrator.benchmark.phase281 import constants
from orchestrator.benchmark.phase281 import references as refs
from tests.benchmark import phase281_parity as parity

REPO_ROOT = parity.REPO_ROOT
CRITERIA_DIR = REPO_ROOT / "benchmarks" / "research_engineering_v1" / "criteria"
EVALUATOR_CLI = REPO_ROOT / "scripts" / "benchmark" / "phase281_evaluate.py"

#: P281 只产出这四态（计划 §5.4.2）；出现别的即契约漂移
P281_VERDICTS = {"BLOCKED_INFRA", "INVALID_ARTIFACT", "FAIL", "PASS"}

M1 = "mujoco_contact_push_v1"


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    """一个可评的数据根（仓外锚 + 0700 目录），只读用；要改根的测试自建。"""
    return parity.build_root(tmp_path_factory.mktemp("p281-root"))


def _new_root(tmp_path: Path) -> dict:
    return parity.build_root(tmp_path / "root")


def _criteria(task_id: str) -> dict:
    name = {
        "mujoco_contact_push_v1": "mujoco_contact_push_v1",
        "libero_env_adapter_v1": "libero_env_adapter_v1",
        "icy_ground_dr_v1": "icy_ground_dr_v1",
    }[task_id]
    return parity.read_json(CRITERIA_DIR / f"{name}.criteria.json")


def _phase0(task_id: str, run_dir: Path, out_dir: Path) -> dict:
    return evaluate_run(task_id, run_dir, out_dir)


def _assert_parity(task_id: str, trial_doc: dict, p0_doc: dict) -> None:
    """逐维比对：Phase 0 的 ``score == 1.0`` 必须逐条等于本评测器的 ``passed``。"""
    assert trial_doc["verdict"] in P281_VERDICTS, trial_doc["verdict"]
    expected = parity.project_status(p0_doc["status"])
    assert trial_doc["verdict"] == expected, (
        f"{task_id}: verdict {trial_doc['verdict']} ≠ 投影后的 Phase 0 "
        f"{p0_doc['status']}→{expected}")
    dims = p0_doc.get("dimensions") or {}
    crit = parity.criterion_map(trial_doc)
    if trial_doc["verdict"] in ("PASS", "FAIL"):
        assert set(crit) >= {f"{ce.DIM_PREFIX}{name}" for name in dims}, (
            f"{task_id}: 判据集未覆盖 Phase 0 的维度集 {sorted(dims)}")
    for name, dim in dims.items():
        key = f"{ce.DIM_PREFIX}{name}"
        if key not in crit:
            # 硬门型判决（BLOCKED/INVALID）不逐条给分，Phase 0 同样只留零分维度
            assert trial_doc["criteria"] == [], (
                f"{task_id}: 判决 {trial_doc['verdict']} 却仍有判据明细")
            continue
        assert crit[key] == (float(dim["score"]) == 1.0), (
            f"{task_id}/{name}: 本评测器 passed={crit[key]} ≠ "
            f"Phase 0 维度分 {dim['score']}")


# --------------------------------------------------------------------------
# AC-P281-1-06..09：判决与拒收面
# --------------------------------------------------------------------------

def test_ac_p281_1_06_no_anchor_blocked(tmp_path: Path):
    """锚不在场/不可信 ⇒ ``BLOCKED_INFRA`` 且 ``criteria`` 为空。"""
    root = tmp_path / "empty-root"
    root.mkdir()
    (root / "private").mkdir()
    for path in (root, root / "private"):
        path.chmod(0o700)

    for label, evaluator in (
        ("锚未加载", ce.CandidateEvaluator(anchor=None, root=root,
                                          anchor_error="锚文件不在场")),
        ("锚未写", ce.CandidateEvaluator(anchor=None, root=root)),
    ):
        trial = parity.build_trial(
            tmp_path / "trial", payload_src=parity.GOLDEN_DIR / "mujoco",
            task_id=M1)
        doc = evaluator.evaluate(trial)
        assert doc["verdict"] == "BLOCKED_INFRA", label
        assert doc["criteria"] == [], label
        # 锚不在时仍要给形态合法的 trial_id（schema 要求）
        rows = cm.result_problems(doc)
        assert rows == [], (label, rows)

    # 内存里有锚但盘上被改：同样 BLOCKED（不采信内存值）
    ctx_root = _new_root(tmp_path)
    path = anchor_mod.anchor_path(ctx_root["root"])
    stale = anchor_mod.load_anchor(path)
    doc_anchor = parity.read_json(path)
    doc_anchor["scoring_sha256"] = "0" * 64
    path.write_text(json.dumps(doc_anchor, ensure_ascii=False), encoding="utf-8")
    evaluator = ce.CandidateEvaluator(anchor=stale, root=ctx_root["root"])
    trial = parity.build_trial(
        tmp_path / "trial2", payload_src=parity.GOLDEN_DIR / "mujoco",
        task_id=M1)
    blocked = evaluator.evaluate(trial)
    assert blocked["verdict"] == "BLOCKED_INFRA"
    assert blocked["criteria"] == []
    assert any("scoring_sha256" in item
               for item in blocked["facts"]["infra_problems"])


def test_ac_p281_1_07_claimed_vs_ledger(tmp_path: Path):
    """``claimed.paid_model_calls`` 与账本不符 ⇒ INVALID_ARTIFACT 并点名该字段。"""
    ctx_root = _new_root(tmp_path)
    root = ctx_root["root"]
    trial = parity.build_trial(
        tmp_path / "trial", payload_src=parity.GOLDEN_DIR / "mujoco",
        task_id=M1, claimed={"paid_model_calls": 5, "coding_rounds": 0,
                             "first_birth_round": 1, "tool_steps": 0,
                             "tokens_in": 0, "tokens_out": 0})
    trial_id = constants.trial_id(M1, "legacy_v1", 0)
    try:
        parity.write_ledger(root, [
            parity.ledger_row(trial_id=trial_id, seq=1, tokens_in=100,
                              tokens_out=50),
            parity.ledger_row(trial_id=trial_id, seq=2, tokens_in=7),
        ])
        doc = ctx_root["evaluator"].evaluate(trial)
    finally:
        parity.write_ledger(root, [])

    assert doc["verdict"] == "INVALID_ARTIFACT", doc["verdict"]
    bad = [row for row in doc["manifest_checks"]
           if row["field"] == "claimed.paid_model_calls"]
    assert bad and bad[0]["equal"] is False, doc["manifest_checks"]
    assert bad[0]["claimed"] == 5 and bad[0]["observed"] == 2
    assert doc["criteria"] == []


def test_ac_p281_1_08_missing_deliverable(tmp_path: Path):
    """删掉 ``deliverables`` 里的一个文件 ⇒ INVALID_ARTIFACT（不给分）。"""
    ctx_root = _new_root(tmp_path)
    trial = parity.build_trial(
        tmp_path / "trial", payload_src=parity.GOLDEN_DIR / "mujoco",
        task_id=M1)
    (trial / "delivered" / "metrics.json").unlink()
    doc = ctx_root["evaluator"].evaluate(trial)
    assert doc["verdict"] == "INVALID_ARTIFACT"
    assert doc["criteria"] == []
    gate = {g["id"]: g for g in doc["gates"]}
    assert gate[ce.GATE_DELIVERABLES]["passed"] is False
    assert "metrics.json" in gate[ce.GATE_DELIVERABLES]["detail"]


def test_ac_p281_1_09_not_born_fail(tmp_path: Path):
    """出生 ``!= BORN`` ⇒ FAIL 且**每条**判据 ``passed=false``（不逐条给分）。"""
    ctx_root = _new_root(tmp_path)
    for status in ("FAILED", "IMPORT_ERROR"):
        trial = parity.build_trial(
            tmp_path / f"trial-{status}", payload_src=parity.GOLDEN_DIR / "mujoco",
            task_id=M1, birth_status=status)
        doc = ctx_root["evaluator"].evaluate(trial)
        assert doc["verdict"] == "FAIL", (status, doc["verdict"])
        assert doc["criteria"], status
        assert all(item["passed"] is False for item in doc["criteria"]), status
        assert all(item["fact"].get("not_scored") is True
                   for item in doc["criteria"]), status

    # 出生件**缺件**不走 AC-09（那句只讲 `status != "BORN"`）：自述了
    # ``first_birth_round`` 却没有任何出生观测可比，属自述不可核验 ⇒ INVALID_ARTIFACT。
    # 这条留在这里，是为了让「缺件」与「件在场但为否」两条路都被钉住——两者都
    # 不给分，但归因不同，报告里不许混为一谈。
    missing = parity.build_trial(
        tmp_path / "trial-missing", payload_src=parity.GOLDEN_DIR / "mujoco",
        task_id=M1, birth_status=None)
    doc = ctx_root["evaluator"].evaluate(missing)
    assert doc["verdict"] == "INVALID_ARTIFACT", doc["verdict"]
    assert doc["criteria"] == []
    gate = {g["id"]: g for g in doc["gates"]}
    assert gate[ce.GATE_BIRTH]["passed"] is False
    assert "birth.json" in gate[ce.GATE_BIRTH]["detail"]
    birth_check = [c for c in doc["manifest_checks"]
                   if c["field"] == "claimed.first_birth_round"][0]
    assert birth_check["equal"] is False and birth_check["observed"] is None


# --------------------------------------------------------------------------
# AC-P281-1-10..12：parity
# --------------------------------------------------------------------------

def _golden_cases() -> "list[dict]":
    return parity.read_json(parity.GOLDEN_DIR / "cases.json")["cases"]


def _negative_cases() -> "list[dict]":
    return parity.read_json(parity.NEGATIVE_DIR / "cases.json")["cases"]


def test_ac_p281_1_10_parity_reference(ctx, tmp_path: Path):
    """三任务参考运行：verdict PASS，且每条判据与 Phase 0 逐条相等。"""
    cases = _golden_cases()
    assert len(cases) == 3, [case["case_id"] for case in cases]
    for case in cases:
        task_id = case["task_id"]
        src = parity.GOLDEN_DIR / case["dir"]
        trial = parity.build_trial(
            tmp_path / case["case_id"], payload_src=src, task_id=task_id)
        doc = ctx["evaluator"].evaluate(trial)
        assert doc["verdict"] == "PASS", (case["case_id"], doc["verdict"],
                                          doc["facts"])
        p0 = _phase0(task_id, src, tmp_path / f"{case['case_id']}-p0")
        _assert_parity(task_id, doc, p0)
        assert all(item["passed"] for item in doc["criteria"])
        assert p0["status"] == "PASS"


def test_ac_p281_1_11_parity_controls(ctx, tmp_path: Path):
    """20 个负控：投影后的判决逐例相符，且有明细时逐维相等。"""
    cases = _negative_cases()
    assert len(cases) >= 16, len(cases)
    for case in cases:
        task_id = case["task_id"]
        src = parity.NEGATIVE_DIR / case["dir"]
        trial = parity.build_trial(
            tmp_path / case["case_id"], payload_src=src, task_id=task_id)
        doc = ctx["evaluator"].evaluate(trial)
        expected = parity.project_status(case["expected_status"])
        assert doc["verdict"] == expected, (
            f"{case['case_id']}: {doc['verdict']} ≠ {expected}"
            f"（Phase 0 期望 {case['expected_status']}）")
        p0 = _phase0(task_id, src, tmp_path / f"{case['case_id']}-p0")
        _assert_parity(task_id, doc, p0)


@pytest.mark.kb_integration
def test_ac_p281_1_10b_parity_pinned_references(tmp_path: Path):
    """**活数据层**版 parity：Phase 2.8.0 ``run_identity.json`` 钉死的那三份 run。

    与 ``_10`` 的关系（计划 §5.6 点名 `benchmark/roadmap_phase0/runs/` 里 pin 的
    运行，而 `_10` 为默认车道跑仓内夹具）：两者断言同一件事——本评测器与 Phase 0
    ``Evaluator`` 在同一输入上**逐条相等**。差别只在输入来源：`_10` 是合成夹具
    （永远可跑），本条是真实参考运行（依赖活数据层，故挂 ``kb_integration``）。
    """
    from orchestrator.benchmark import paths as p0_paths

    root = parity.build_root(tmp_path / "root")
    for task_id in ("mujoco_contact_push_v1", "libero_env_adapter_v1",
                    "icy_ground_dr_v1"):
        run = p0_paths.pinned_run(None, task_id)
        target = root["root"] / "references" / task_id
        summary = refs.build_reference(root["root"], task_id, run)
        assert summary["run_id"] == run.name
        assert summary["run_status"] == "PASS", (task_id, summary["run_status"])
        trial = Path(summary["target"])
        assert trial == target

        doc = root["evaluator"].evaluate(trial)
        assert doc["verdict"] == "PASS", (task_id, doc["verdict"], doc["facts"])
        p0 = _phase0(task_id, run, tmp_path / f"{task_id}-p0")
        _assert_parity(task_id, doc, p0)
        assert all(item["passed"] for item in doc["criteria"])


# -- 定向变异 --------------------------------------------------------------

_TELEMETRIES = ("telemetry.jsonl", "repro/same_seed/telemetry.jsonl",
                "repro/cross_seed/telemetry.jsonl")
_METRICS = ("metrics.json", "repro/same_seed/metrics.json",
            "repro/cross_seed/metrics.json")


def _load_lines(path: Path) -> "list[dict]":
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _dump_lines(path: Path, rows: "list[dict]") -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n"
                            for row in rows), encoding="utf-8")


def _recompute_metrics(root: Path) -> None:
    """把三份 metrics 重算到与 telemetry 一致（变异只改物理量，不改一致性）。"""
    criteria = _criteria(M1)
    for rel_tel, rel_met in zip(_TELEMETRIES, _METRICS):
        facts = scoring.mujoco_facts(
            {"telemetry.jsonl": _load_lines(root / rel_tel)}, criteria)
        rec = facts["recomputed"]
        path = root / rel_met
        doc = parity.read_json(path) if path.is_file() else {}
        doc.update({key: rec[key] for key in
                    ("endpoint_distance_m", "max_penetration_m",
                     "contact_force_peak_n", "steps")})
        parity.write_json(path, doc)


def _apply_each(root: Path, fn) -> None:
    for rel in _TELEMETRIES:
        path = root / rel
        rows = _load_lines(path)
        fn(rows)
        _dump_lines(path, rows)


def _mut_target_shift(root: Path) -> None:
    _apply_each(root, lambda rows: [row.update(
        {"target_pos": [0.03, 0.0, 0.025]}) for row in rows])
    _recompute_metrics(root)


def _mut_steps_short(root: Path) -> None:
    _apply_each(root, lambda rows: rows.__delitem__(slice(533, None)))
    _recompute_metrics(root)


def _mut_penetration(root: Path) -> None:
    _apply_each(root, lambda rows: [row.update(
        {"max_penetration": 0.003}) for row in rows])
    _recompute_metrics(root)


def _mut_force_high(root: Path) -> None:
    _apply_each(root, lambda rows: [row.update(
        {"contact_force_norm": 3.0}) for row in rows])
    _recompute_metrics(root)


def _mut_force_low(root: Path) -> None:
    _apply_each(root, lambda rows: [row.update(
        {"contact_force_norm": 0.1}) for row in rows])
    _recompute_metrics(root)


def _mut_nonfinite(root: Path) -> None:
    def patch(rows):
        middle = len(rows) // 2
        # 用 Infinity 而不是 NaN：NaN != NaN 会让「同种子 telemetry 逐字段相等」
        # 因浮点语义而非缺陷翻面，变异就不再「恰好翻一条」。
        rows[middle]["cube_pos"] = [float("inf"), 0.0, 0.025]
    _apply_each(root, patch)
    _recompute_metrics(root)


def _mut_repro_same_seed(root: Path) -> None:
    path = root / "repro/same_seed/telemetry.jsonl"
    rows = _load_lines(path)
    rows[10]["cube_pos"] = [rows[10]["cube_pos"][0] + 0.001, 0.0, 0.025]
    _dump_lines(path, rows)


def _mut_repro_cross_seed(root: Path) -> None:
    path = root / "repro/cross_seed/metrics.json"
    doc = parity.read_json(path)
    doc["endpoint_distance_m"] = 0.02
    parity.write_json(path, doc)


def _mut_evidence_scene(root: Path) -> None:
    (root / "scene_spec.json").write_text("null\n", encoding="utf-8")


def _mut_evidence_step_dup(root: Path) -> None:
    def patch(rows):
        rows[5]["step"] = rows[4]["step"]
    _apply_each(root, patch)
    _recompute_metrics(root)


MUTATIONS = (
    ("functional_target_shift", _mut_target_shift, "DIM_functional_correctness"),
    ("functional_steps_short", _mut_steps_short, "DIM_functional_correctness"),
    ("physics_penetration", _mut_penetration, "DIM_physics_validity"),
    ("physics_force_high", _mut_force_high, "DIM_physics_validity"),
    ("physics_force_low", _mut_force_low, "DIM_physics_validity"),
    ("physics_nonfinite", _mut_nonfinite, "DIM_physics_validity"),
    ("repro_same_seed_tel", _mut_repro_same_seed, "DIM_reproducibility"),
    ("repro_cross_seed_metrics", _mut_repro_cross_seed, "DIM_reproducibility"),
    ("evidence_scene_spec", _mut_evidence_scene, "DIM_evidence_completeness"),
    ("evidence_step_dup", _mut_evidence_step_dup, "DIM_evidence_completeness"),
)


def test_ac_p281_1_12_mutation_sensitivity(ctx, tmp_path: Path):
    """10 个定向变异各翻**恰好一条**判据；无变异一条都不翻。"""
    baseline_src = parity.GOLDEN_DIR / "mujoco"
    baseline_trial = parity.build_trial(
        tmp_path / "baseline", payload_src=baseline_src, task_id=M1)
    baseline = parity.criterion_map(ctx["evaluator"].evaluate(baseline_trial))
    assert baseline and all(baseline.values()), baseline

    assert len(MUTATIONS) == 10, len(MUTATIONS)
    # AC 只要求「每个变异各翻恰一条」；同一判据允许多个变异（如功能维既有位移
    # 也有步数），但四条判据必须**都被打到**，否则「敏感」只覆盖一部分。
    assert {dim for _, _, dim in MUTATIONS} == {
        "DIM_functional_correctness", "DIM_physics_validity",
        "DIM_reproducibility", "DIM_evidence_completeness"}

    for name, mutate, expected_flip in MUTATIONS:
        payload = tmp_path / f"mut-{name}" / "payload"
        shutil.copytree(baseline_src, payload,
                        ignore=shutil.ignore_patterns("__pycache__", ".omc"))
        mutate(payload)
        trial = parity.build_trial(
            tmp_path / f"mut-{name}" / "trial", payload_src=payload,
            task_id=M1, source_note=f"mutation:{name}")
        doc = ctx["evaluator"].evaluate(trial)
        got = parity.criterion_map(doc)

        flipped = sorted(key for key in got if not got[key])
        assert flipped == [expected_flip], (
            f"{name}: 期望只翻 {expected_flip}，实翻 {flipped}")
        assert doc["verdict"] == "FAIL", (name, doc["verdict"])


# --------------------------------------------------------------------------
# AC-P281-1-13..15：执行面纪律
# --------------------------------------------------------------------------

def test_references_build_is_faithful(ctx, tmp_path: Path):
    """``references.build_reference``：逐件搬运、出生件照抄、排除项不搬、判决 PASS。

    这条不是计划点名的 AC，而是本 Task 新增的生产件（``references.py``，§5.7
    步骤 4 需要 Trial 形态的参考快照）的自检：不测它，「参考快照怎么来的」就
    只剩一次手工命令可查。夹具 ``libero`` 恰好是运行形态（含
    ``birth/functional_birth.json`` 与 ``run_manifest.json``），与真实参考运行同形。
    """
    src = parity.GOLDEN_DIR / "libero"
    root = tmp_path / "root"
    parity.build_root(root)          # 只借目录族与锚

    summary = refs.build_reference(root, "libero_env_adapter_v1", src)
    trial = Path(summary["target"])
    assert summary["run_status"] == "PASS" or summary["run_status"] is None
    assert summary["birth_status"] == "BORN"

    expected = set(refs.payload_relpaths(src))
    delivered = {p.relative_to(trial / "delivered").as_posix()
                 for p in (trial / "delivered").rglob("*") if p.is_file()}
    assert delivered == expected
    assert "birth/functional_birth.json" in delivered      # L1 判据要读它
    assert not any(rel.startswith("candidate/") for rel in delivered)

    birth = parity.read_json(trial / "birth.json")
    assert birth == parity.read_json(src / "birth" / "functional_birth.json")
    manifest = parity.read_json(trial / cm.MANIFEST_NAME)
    assert manifest["claimed"]["paid_model_calls"] == 0
    assert {row["relpath"] for row in manifest["deliverables"]} == {
        f"{cm.OUTPUT_PREFIX}{rel}" for rel in expected}

    evaluator = ce.CandidateEvaluator(
        anchor=anchor_mod.load_anchor(anchor_mod.anchor_path(root)), root=root)
    doc = evaluator.evaluate(trial)
    assert doc["verdict"] == "PASS", (doc["verdict"], doc["facts"])
    assert doc["evaluator"]["isolated"] is False   # 进程内评测：不在 -I 子进程里


def _load_cli():
    spec = importlib.util.spec_from_file_location("p281_evaluate_cli",
                                                  EVALUATOR_CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ac_p281_1_13_child_process_hygiene(ctx, tmp_path: Path):
    """子进程 argv 含 ``-I``、cwd 不在 trial 内、env 无任何 ``ANTHROPIC_*``。"""
    cli = _load_cli()
    trial = parity.build_trial(
        tmp_path / "trial", payload_src=parity.GOLDEN_DIR / "mujoco",
        task_id=M1)
    out = tmp_path / "result.json"
    anchor_path = anchor_mod.anchor_path(ctx["root"])

    summary = cli.run_child(trial_dir=trial, anchor=anchor_path, out=out,
                            root=ctx["root"], python=sys.executable,
                            repo_root=REPO_ROOT)
    assert summary["rc"] == 0, summary["stderr"]
    argv = summary["argv"]
    assert "-I" in argv
    assert argv[0] == sys.executable
    assert str(trial) in argv          # 只以**文件路径**身份出现
    assert summary["cwd_removed"] is True

    env = cli.child_env()
    assert not [key for key in env if key.startswith("ANTHROPIC_")]
    assert "SIMPRAXIS_ALLOW_PAID" not in env
    assert set(env) <= set(cli.CHILD_ENV_ALLOWLIST)

    doc = parity.read_json(out)
    assert doc["verdict"] == "PASS"
    assert doc["evaluator"]["isolated"] is True
    assert doc["evaluator"]["pid"] > 0
    assert out.stat().st_mode & 0o777 == 0o600

    # 子进程见过凭据必须拒开工（防「父进程忘了过滤」）
    proc = subprocess.run(
        [sys.executable, "-I", "-c",
         "import os,sys;os.environ['ANTHROPIC_BASE_URL']='x';"
         "sys.path.insert(0,sys.argv[1]);"
         "from orchestrator.benchmark.phase281.evaluate_child import run;"
         "raise SystemExit(run(sys.argv[2:]))",
         str(REPO_ROOT), "--trial-dir", str(trial), "--anchor", str(anchor_path),
         "--out", str(tmp_path / "refused.json")],
        capture_output=True, text=True, check=False)
    assert proc.returncode == 2, proc.stderr
    assert "ANTHROPIC_" in proc.stderr


def test_ac_p281_1_14_no_candidate_exec():
    """评测器源码里不存在执行候选代码的入口（静态扫描，白纸黑字）。"""
    module_dir = REPO_ROOT / "orchestrator" / "benchmark" / "phase281"
    targets = [module_dir / name for name in
               ("candidate_evaluator.py", "candidate_manifest.py", "anchor.py",
                "evaluate_child.py", "references.py")]
    forbidden = ("sys.path.insert", "importlib", "exec(", "eval(")
    for path in targets:
        assert path.is_file(), path
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{path.name}: 出现 {token!r}"

    # 评测器也不得 import Phase 0 的 Evaluator 类（独立性：代码路径不复用）
    evaluator_text = targets[0].read_text(encoding="utf-8")
    assert "from orchestrator.benchmark.evaluator import" not in evaluator_text
    assert "import orchestrator.benchmark.evaluator" not in evaluator_text


def test_ac_p281_1_15_result_schema(ctx, tmp_path: Path):
    """``evaluate()`` 的输出回灌 schema 校验器必须通过（四种判决都验）。"""
    docs = []

    pass_trial = parity.build_trial(
        tmp_path / "pass", payload_src=parity.GOLDEN_DIR / "mujoco", task_id=M1)
    docs.append(ctx["evaluator"].evaluate(pass_trial))

    fail_trial = parity.build_trial(
        tmp_path / "fail", payload_src=parity.NEGATIVE_DIR / "mujoco/n01_no_motion",
        task_id=M1)
    docs.append(ctx["evaluator"].evaluate(fail_trial))

    broken = parity.build_trial(
        tmp_path / "broken", payload_src=parity.GOLDEN_DIR / "mujoco",
        task_id=M1)
    (broken / cm.MANIFEST_NAME).write_text("{}", encoding="utf-8")
    docs.append(ctx["evaluator"].evaluate(broken))

    blocked = ce.CandidateEvaluator(anchor=None, root=ctx["root"]).evaluate(
        pass_trial)
    docs.append(blocked)

    verdicts = {doc["verdict"] for doc in docs}
    assert verdicts == {"PASS", "FAIL", "INVALID_ARTIFACT", "BLOCKED_INFRA"}, verdicts
    for doc in docs:
        assert cm.result_problems(doc) == [], (doc["verdict"],
                                               cm.result_problems(doc))
        cm.validate_result(doc)          # 抛错即失败
        assert doc["schema"] == cm.RESULT_SCHEMA
        assert doc["evaluated_at"].endswith("Z")
        assert doc["criteria"] == [] or doc["verdict"] in ("PASS", "FAIL")
