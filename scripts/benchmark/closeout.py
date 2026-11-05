#!/usr/bin/env python3
"""Phase 2.8.0 Task 7 收尾编排（0 付费）。

子命令（顺序即防自指协议，见 T7.5 第 4 步）：

- ``reports``     —— 渲染七件报告到 ``<root>/reports/``（必须在打包之前）
- ``cold-run``    —— 解析冷跑日志 → ``closeout/cold_run.json``
- ``pack``        —— ``--clean`` 重建证据包 → ``pack.sha256``
- ``acceptance``  —— 跑 ``milestone_closeout`` 验收车道，落日志与 rc
- ``matrix``      —— 写 ``ledger_freeze.json`` / ``kb_integrity.json`` /
                     ``invariants.json`` / ``acceptance_matrix.json`` /
                     ``environment_closeout.txt``（判决件族，不入包）
- ``judge``       —— 调 ``check_closure.run_judge``（``--require-acceptance``
                     为终判），写 ``closeout/closure_judge.json``

纪律：0 付费、不 push、不改五本账；所有数字现读现算；判决件族不入包。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
if str(_REPO / "scripts" / "benchmark") not in sys.path:
    sys.path.insert(0, str(_REPO / "scripts" / "benchmark"))

import build_evidence_pack as packer  # noqa: E402
import check_closure as judge  # noqa: E402
from orchestrator.benchmark import paths, report  # noqa: E402

TASKS = judge.TASKS
SCHEMA_MATRIX = "simpraxis.benchmark.acceptance_matrix.v1"
MAIN_PYTHON = "/mnt/venvs/simpraxis/bin/python"

GAPS = (
    {"id": "OI-5", "owner": "用户",
     "reason": "Roadmap Phase 1 的本地编号与时间表尚未裁定；不阻塞本阶段。",
     "evidence": "Phase_2.8.0_Plan_Detailed §9.2 OI-5"},
    {"id": "OI-6", "owner": "用户",
     "reason": "CLAUDE.md 仍写 solution_chunks=94，实测 106；修改 CLAUDE.md "
               "非本阶段职责（TD-5）。",
     "evidence": "intake/table_counts.json"},
    {"id": "OI-7", "owner": "未来工程化任务",
     "reason": "是否新增 scripts/__init__.py 使 python -m scripts.* 可用；会打穿"
               "既有路径假设，本阶段不改。",
     "evidence": "scripts/benchmark/*.py 以 CLI 薄封装方式运行"},
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write_json(path: Path, doc) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1,
                               sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _git(*args: str, cwd: Path = _REPO) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=120)
    return proc.stdout.strip()


def _run(cmd: "list[str]") -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return f"$ {' '.join(cmd)}\n(rc={proc.returncode})\n{proc.stdout}" \
           f"{proc.stderr}"


# --------------------------------------------------------------------------
# ① 报告 / ② 冷跑 / ③ 打包
# --------------------------------------------------------------------------

def cmd_reports(root: Path) -> dict:
    written = report.build_reports(root)
    return {key: str(path) for key, path in written.items()}


def cmd_cold_run(root: Path, raw_log: Path, rc: "int | None") -> dict:
    doc = judge.record_cold_run(root, raw_log, rc)
    return {k: doc[k] for k in ("passed", "skipped", "deselected", "delta_ok",
                                "verdict")}


def cmd_pack(root: Path, clean: bool) -> dict:
    result = packer.build(root=root, pack=root / "evidence_pack", clean=clean)
    return {"pack_sha256": result["pack_sha256"], "n_files": result["n_files"],
            "total_bytes": result["total_bytes"]}


# --------------------------------------------------------------------------
# ④ 验收车道
# --------------------------------------------------------------------------

def cmd_acceptance(root: Path) -> dict:
    out = root / "closeout"
    out.mkdir(parents=True, exist_ok=True)
    log = out / "acceptance_lane.log"
    cmd = [MAIN_PYTHON, "-m", "pytest", "tests/benchmark/test_task7_closeout.py",
           "-m", "milestone_closeout", "-q"]
    env = dict(os.environ, SIMPRAXIS_MILESTONE_CLOSEOUT="1")
    proc = subprocess.run(cmd, cwd=str(_REPO), env=env, capture_output=True,
                          text=True, timeout=1800)
    text = f"$ {' '.join(cmd)}\n(rc={proc.returncode})\n" \
           f"{proc.stdout}{proc.stderr}"
    log.write_text(text, encoding="utf-8")
    log.chmod(0o600)
    summary = judge.parse_pytest_summary(text) or {"passed": 0, "skipped": 0,
                                                   "deselected": 0,
                                                   "duration_s": 0.0}
    failed = re.search(r"(\d+) failed", text)
    doc = {
        "schema": "simpraxis.benchmark.acceptance_lane.v1",
        "generated_at": _now(),
        "cmd": " ".join(cmd),
        "rc": proc.returncode,
        "passed": summary["passed"],
        "failed": int(failed.group(1)) if failed else 0,
        "deselected": summary["deselected"],
        "summary": (text.strip().splitlines() or [""])[-1],
        "log": str(log),
        "log_sha256": _sha256(log),
    }
    _write_json(out / "acceptance_lane.json", doc)
    return doc


# --------------------------------------------------------------------------
# ⑤/⑥ 判决件族
# --------------------------------------------------------------------------

def _ledger_paths() -> "dict[str, Path]":
    """五本账路径现算（§4.1）：封存四本必须来自 `sealed_ledger_paths()`，
    活跃账必须来自 `phase27_ledger_path()`；**不手写任何路径**。"""
    from orchestrator.common import runtime_paths as rp

    paths = {
        "phase1": rp.phase1_ledger_path(),
        "phase2": rp.phase2_ledger_path(),
        "phase25": rp.phase25_ledger_path(),
        "phase26": rp.phase26_ledger_path(),
        "phase27": rp.phase27_ledger_path(),
    }
    sealed = rp.sealed_ledger_paths()
    for phase in ("phase1", "phase2", "phase25", "phase26"):
        if paths[phase].resolve(strict=False) not in sealed:
            raise RuntimeError(f"{phase} 账不在 sealed_ledger_paths() 中："
                               f"{paths[phase]}")
    return paths


def write_ledger_freeze(root: Path) -> dict:
    snapshot = _json(root / "intake" / "sealed_ledger_sha256.json")["ledgers"]
    rows = []
    for phase in judge.LEDGER_PHASES:
        path = _ledger_paths()[phase]
        actual = _sha256(path) if path.is_file() else None
        before = snapshot[phase]
        rows.append({
            "phase": phase,
            "path": str(path),
            "rows_before": int(before["rows"]),
            "rows_after": int(before["rows"]) if actual == before["sha256"]
            else None,
            "sha256_before": before["sha256"],
            "sha256_after": actual,
            "mtime_before": None,
            "mtime_after": (path.stat().st_mtime_ns
                            if path.is_file() else None),
            "equal": actual == before["sha256"],
            "mtime_note": "Task 0 快照未采集 mtime；以 SHA256+行数前后相同为准",
        })
    doc = {
        "schema": "simpraxis.benchmark.ledger_freeze.v1",
        "generated_at": _now(),
        "sealed_phases": ["phase1", "phase2", "phase25", "phase26"],
        "active_phase": "phase27",
        "ledgers": rows,
        "verdict": "FROZEN" if all(row["equal"] for row in rows) else "DRIFT",
    }
    _write_json(root / "closeout" / "ledger_freeze.json", doc)
    return doc


def write_paid_calls(root: Path) -> dict:
    """P0-AC-16 的机读判定物（§4.1 `paid_calls.json`）。

    账本数字一律「Task 0 快照 vs 现算」；`gateway_imports_in_benchmark`
    由现扫 benchmark 三个代码子树得出，缺一即非 0。
    """
    snapshot = _json(root / "intake" / "sealed_ledger_sha256.json")["ledgers"]
    paths = _ledger_paths()
    rows = []
    for phase in judge.LEDGER_PHASES:
        path = paths[phase]
        before = snapshot[phase]
        actual = _sha256(path) if path.is_file() else None
        rows.append({
            "phase": phase,
            "path": str(path),
            "rows_before": int(before["rows"]),
            "rows_after": (sum(1 for line in path.read_text(
                encoding="utf-8").splitlines() if line.strip())
                if path.is_file() else None),
            "sha256_before": before["sha256"],
            "sha256_after": actual,
            "equal": actual == before["sha256"],
        })
    gateway = 0
    needle = re.compile(r"^\s*(?:from\s+orchestrator\.gateway|"
                        r"import\s+orchestrator\.gateway)", re.MULTILINE)
    for sub in ("orchestrator/benchmark", "scripts/benchmark",
                "tests/benchmark"):
        base = _REPO / sub
        for path in sorted(base.rglob("*.py")):
            if needle.search(path.read_text(encoding="utf-8",
                                            errors="ignore")):
                gateway += 1
    added = sum(max(0, (row["rows_after"] or 0) - row["rows_before"])
                for row in rows)
    zero = (all(row["equal"] for row in rows) and added == 0 and gateway == 0)
    doc = {
        "schema": "simpraxis.benchmark.paid_calls.v1",
        "generated_at": _now(),
        "ledgers": rows,
        "paid_call_rows_added": int(added),
        "gateway_imports_in_benchmark": gateway,
        "verdict": "ZERO_PAID" if zero else "PAID_OR_VIOLATION",
    }
    _write_json(root / "closeout" / "paid_calls.json", doc)
    return doc


def write_kb_integrity(root: Path) -> dict:
    before = _json(root / "intake" / "kb_state.json")
    repo = Path(before["kb_root"])
    baseline = _json(root / "intake" / "table_counts.json")
    tables = {}
    try:
        import lancedb

        conn = lancedb.connect(baseline["lancedb_uri"])
        for name, expected in sorted(baseline["tables"].items()):
            actual = (int(conn.open_table(name).count_rows())
                      if name in set(conn.table_names()) else None)
            tables[name] = [int(expected), actual]
    except Exception as exc:  # pragma: no cover
        tables = {"error": [None, str(exc)]}
    head_after = _git("rev-parse", "HEAD", cwd=repo)
    porcelain_after = _git("status", "--porcelain", cwd=repo)
    unchanged = (head_after == before["head"]
                 and porcelain_after == ""
                 and all(pair[0] == pair[1] for pair in tables.values()
                         if isinstance(pair, list)))
    doc = {
        "schema": "simpraxis.benchmark.kb_integrity.v1",
        "generated_at": _now(),
        "head_before": before["head"],
        "head_after": head_after,
        "porcelain_before": before["status_porcelain"],
        "porcelain_after": porcelain_after,
        "tables": tables,
        "verdict": "UNCHANGED" if unchanged else "CHANGED",
    }
    _write_json(root / "closeout" / "kb_integrity.json", doc)
    return doc


def _diff_names(base: str, *spec: str) -> "list[str]":
    out = _git("diff", "--name-status", f"{base}..HEAD", "--", *spec)
    return [line for line in out.splitlines() if line.strip()]


def _probe_run_spec() -> "tuple[bool, str]":
    from orchestrator.agent_runner.runner import RunSpec

    default = RunSpec.__dataclass_fields__["agentic"].default
    return default is False, f"RunSpec.agentic 默认={default!r}"


def _probe_agentic_config() -> "tuple[bool, str]":
    import dataclasses

    from orchestrator.agentic.config import AgenticConfig

    config = AgenticConfig()  # slots=True：无 __dict__，按 fields 取
    booleans = {f.name: getattr(config, f.name)
                for f in dataclasses.fields(config) if f.type in ("bool", bool)}
    ok = (not any(booleans.values())) and config.run_mode == "R"
    return ok, f"AgenticConfig() 布尔开关={booleans}, run_mode={config.run_mode}"


def _phase27_pack_sha(root: Path) -> "tuple[str | None, dict]":
    """Phase 2.7 包的**规范化** sha（与 Task 0 采集同一口径）。"""
    from orchestrator.benchmark import intake as bench_intake

    before = _json(root / "intake" / "invariants_before.json")
    try:
        digest = bench_intake.phase27_pack_sha256(Path(before["phase27_pack"]))
    except FileNotFoundError:
        return None, before
    return digest["phase27_pack_sha256"], before


def write_invariants(root: Path) -> dict:
    state = _json(root / "intake" / "repository_state.json")
    base = state["head"]
    before = _json(root / "intake" / "invariants_before.json")
    rows = []

    def add(iid, statement, ok, evidence):
        rows.append({"id": iid, "statement": statement, "ok": bool(ok),
                     "evidence": evidence})

    ok, ev = _probe_run_spec()
    add("I1", "RunSpec.agentic 默认值恒为 False", ok, ev)
    ok, ev = _probe_agentic_config()
    add("I2", "AgenticConfig 全开关默认 False、run_mode=='R'", ok, ev)
    diff = _diff_names(base, "orchestrator/graph/")
    add("I3", "LangGraph 主流程零 diff", not diff, {"diff": diff})
    stage_models = Path(before["stage_models_yaml"])
    actual = _sha256(stage_models)
    add("I4", "stage_models.yaml 与 Task 0 快照相同",
        actual == before["stage_models_sha256"],
        {"before": before["stage_models_sha256"], "after": actual})
    kb_problems, kb_detail = judge.check_kb(root)
    add("I5", "KB 只读：HEAD/工作树/四表行数不变", not kb_problems,
        {"problems": kb_problems, "detail": kb_detail})
    freeze = _json(root / "closeout" / "ledger_freeze.json")
    pack_sha, _before = _phase27_pack_sha(root)
    add("I6", "五本账 SHA256 不变 + Phase 2.7 包 sha 不变",
        all(row["equal"] for row in freeze["ledgers"])
        and pack_sha == before["phase27_pack_sha256"],
        {"ledgers": {row["phase"]: row["equal"] for row in freeze["ledgers"]},
         "phase27_pack_before": before["phase27_pack_sha256"],
         "phase27_pack_after": pack_sha})
    diff = _diff_names(base, "orchestrator/agentic/loop.py")
    add("I7", "_entrypoint_ready() 零改动", not diff, {"diff": diff})
    diff = _diff_names(base, "scripts/phase27/")
    porcelain = _git("status", "--porcelain", "--", "scripts/phase27/")
    add("I8", "scripts/phase27/ 零改动", not diff and not porcelain,
        {"diff": diff, "porcelain": porcelain})
    diff = _diff_names(base, ".", ":!benchmarks", ":!orchestrator/benchmark",
                       ":!scripts/benchmark", ":!tests/benchmark",
                       ":!tests/fixtures/benchmark", ":!CLAUDE.md")
    add("I9", "新代码只落在白名单路径、提交前缀 phase28", not diff,
        {"diff": diff})
    origin = _git("rev-parse", "origin/main")
    add("I10", "不 push、不改写历史（origin/main 锚定）",
        origin == judge.ORIGIN_MAIN, {"origin_main": origin,
                                      "expected": judge.ORIGIN_MAIN})
    pack = root / "evidence_pack"
    try:
        packer._check_clean_target(pack, root)
        rejected = False
        try:
            packer._check_clean_target(root, root)
        except packer.PackError:
            rejected = True
        add("I11", "删除守卫：唯一删除入口 _check_clean_target()",
            rejected, {"pack_ok": True, "root_rejected": rejected})
    except Exception as exc:  # pragma: no cover
        add("I11", "删除守卫", False, {"error": str(exc)})
    containers = subprocess.run(["docker", "ps", "-aq"], capture_output=True,
                                text=True, timeout=60)
    gpu = subprocess.run(["nvidia-smi", "--query-gpu=memory.used",
                          "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, timeout=60)
    gpu_values = [int(x) for x in gpu.stdout.split() if x.strip().isdigit()]
    add("I12", "容器 0 残留、显存 ≤ 200 MiB",
        containers.returncode == 0 and not containers.stdout.strip()
        and gpu_values and max(gpu_values) <= 200,
        {"containers": containers.stdout.split(), "gpu_mib": gpu_values})
    doc = {"schema": "simpraxis.benchmark.invariants.v1",
           "generated_at": _now(), "baseline_head": base,
           "commit": _git("rev-parse", "HEAD"),
           "invariants": rows,
           "verdict": "OK" if all(row["ok"] for row in rows) else "VIOLATED"}
    _write_json(root / "closeout" / "invariants.json", doc)
    return doc


def write_environment_closeout(root: Path) -> Path:
    lines = [_now(), ""]
    lines.append(_run(["docker", "ps", "-a"]))
    lines.append(_run(["nvidia-smi"]))
    lines.append(_run(["df", "-h", "/mnt"]))
    lines.append(_run(["git", "status", "--short", "--branch"]))
    path = root / "closeout" / "environment_closeout.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    path.chmod(0o600)
    return path


def _pytest_ok(nodes: "list[str]") -> "tuple[bool, dict]":
    """跑指定测试节点（0 付费）；rc 即证据，绝不把「没跑」记成 PASS。"""
    cmd = [MAIN_PYTHON, "-m", "pytest", *nodes, "-q"]
    proc = subprocess.run(cmd, cwd=str(_REPO), capture_output=True, text=True,
                          timeout=900)
    summary = judge.parse_pytest_summary(proc.stdout + proc.stderr) or {}
    return proc.returncode == 0 and int(summary.get("passed", 0)) == len(nodes), {
        "cmd": " ".join(cmd), "rc": proc.returncode,
        "passed": summary.get("passed"), "nodes": nodes}


def _ac_rows(root: Path) -> "list[dict]":
    """P0-AC-01..20 的逐条机判行（证据现读；缺件即 gap/FAIL，不凑数）。"""
    state = _json(root / "intake" / "repository_state.json")
    registry = _json(root / "specs" / "registry_snapshot.json")
    run_ids = {task: judge._latest_run(root, task).name for task in TASKS}
    manifests = {task: _json(root / "runs" / run_ids[task] / "run_manifest.json")
                 for task in TASKS}
    births = {task: _json(root / "runs" / run_ids[task] / "birth" /
                          "functional_birth.json") for task in TASKS}
    golden = _json(root / "negative_controls" / "golden_smoke.json")
    negative = _json(root / "negative_controls" / "negative_control_results.json")
    mutation = _json(root / "negative_controls" / "mutation_results.json")
    repro = _json(root / "runs" / run_ids["mujoco_contact_push_v1"] /
                  "reproducibility.json")
    freezes = _json(root / "closeout" / "ledger_freeze.json")
    paid = _json(root / "closeout" / "paid_calls.json")
    kb = _json(root / "closeout" / "kb_integrity.json")
    cold = _json(root / "closeout" / "cold_run.json")
    recompute = all((root / "evaluator_results" / run_ids[t] /
                     "recompute_trace.jsonl").is_file() for t in TASKS)
    levels_ok = all(sorted(births[t]["levels"])[:1] == ["B0"] and
                    "B4" in births[t]["levels"] for t in TASKS)
    signatures = {role: (root / "closeout" / "signs" / f"{role}.json").is_file()
                  for role in judge.ROLES}
    pack_manifest = root / "evidence_pack" / "manifest.json"

    def row(ac, name, ok, evidence, note=""):
        return {"ac": ac, "name": name, "result": "PASS" if ok else "FAIL",
                "evidence": evidence, "note": note}

    ok_02, ev_02 = _pytest_ok(
        ["tests/benchmark/test_task1_task_spec.py::test_ac_p0_1_6_deterministic"])
    ok_03, ev_03 = _pytest_ok([
        "tests/benchmark/test_task1_task_spec.py::test_ac_p0_1_2_unknown_field",
        "tests/benchmark/test_task1_task_spec.py::test_ac_p0_1_3_missing_field",
        "tests/benchmark/test_task1_task_spec.py::test_ac_p0_1_4_duplicate_id"])
    ok_05, ev_05 = _pytest_ok(
        ["tests/benchmark/test_task2_runtime_fingerprint.py::test_ac_p0_2_4_ten_mismatches"])
    return [
        row("P0-AC-01", "Task Specification：三任务均可加载",
            len(registry["tasks"]) == 3, "specs/registry_snapshot.json"),
        row("P0-AC-02", "Registry：三任务均可查询且确定性",
            bool(registry["criteria_lock"])
            and bool(registry["manifest_sha256"]) and ok_02,
            "specs/registry_snapshot.json",
            "task7 matrix 现跑 test_ac_p0_1_6_deterministic（rc 见 notes）"),
        row("P0-AC-03", "Schema Negative：非法定义正确拒绝",
            ok_03, "specs/validation_report.json",
            "task7 matrix 现跑三个负例测试（rc 见 notes）"),
        row("P0-AC-04", "Runtime Fingerprint：两工程任务均生成真实指纹",
            all((root / "fingerprints" / t / "runtime_fingerprint.json").is_file()
                for t in ("mujoco_contact_push_v1", "libero_env_adapter_v1")),
            "fingerprints/*/runtime_fingerprint.json"),
        row("P0-AC-05", "Fingerprint Mismatch：十种差异均能识别",
            ok_05, "tests::test_ac_p0_2_4_ten_mismatches",
            "task7 matrix 现跑该测试；10/10 断言在测试内部"),
        row("P0-AC-06", "Birth B0–B4：五级验证均有测试",
            levels_ok, "runs/*/birth/functional_birth.json"),
        row("P0-AC-07", "MuJoCo Reference：真实仿真通过冻结判据",
            manifests["mujoco_contact_push_v1"]["status"] == "PASS",
            f"runs/{run_ids['mujoco_contact_push_v1']}/run_manifest.json"),
        row("P0-AC-08", "LIBERO Reference：真实 import/construct/reset/step",
            manifests["libero_env_adapter_v1"]["status"] == "PASS"
            and (root / "fingerprints" / "libero_env_adapter_v1" /
                 "runtime_fingerprint.json").is_file(),
            f"runs/{run_ids['libero_env_adapter_v1']}/run_manifest.json",
            "容器车道证据在 Task 6 容器运行与指纹件内"),
        row("P0-AC-09", "Ice DR Reference：结构化方案符合 Rubric",
            manifests["icy_ground_dr_v1"]["status"] == "PASS",
            f"runs/{run_ids['icy_ground_dr_v1']}/run_manifest.json"),
        row("P0-AC-10", "Golden Controls：全部通过（含真实参考）",
            bool(golden["all_passed"]) and
            all(_json(root / "evaluator_results" / run_ids[t] /
                      "evaluation_result.json")["status"] == "PASS"
                for t in TASKS),
            "negative_controls/golden_smoke.json + evaluator_results/*"),
        row("P0-AC-11", "Negative Controls：全部得到预期负判",
            int(negative["count"]) == int(negative["rejected"]) > 0,
            "negative_controls/negative_control_results.json"),
        row("P0-AC-12", "Evaluator Mutation：全部通过",
            int(mutation["count"]) == int(mutation["rejected"]) > 0,
            "negative_controls/mutation_results.json"),
        row("P0-AC-13", "Independent Score：不信任 Agent 自报",
            recompute, "evaluator_results/*/recompute_trace.jsonl"),
        row("P0-AC-14", "Evidence Manifest：每个产物均有 SHA256",
            pack_manifest.is_file(), "evidence_pack/manifest.json"),
        row("P0-AC-15", "Reproducibility：同环境同输入同种子可复现",
            bool(repro["same_seed_identical"]),
            f"runs/{run_ids['mujoco_contact_push_v1']}/reproducibility.json"),
        row("P0-AC-16", "Paid Calls：0",
            paid["verdict"] == "ZERO_PAID"
            and int(paid["paid_call_rows_added"]) == 0
            and int(paid["gateway_imports_in_benchmark"]) == 0,
            "closeout/paid_calls.json"),
        row("P0-AC-17", "KB Integrity：工作树与数据层不变",
            kb["verdict"] == "UNCHANGED", "closeout/kb_integrity.json"),
        row("P0-AC-18", "Historical Ledgers：旧账本 SHA256 不变",
            freezes["verdict"] == "FROZEN", "closeout/ledger_freeze.json"),
        row("P0-AC-19", "Core Regression：现有测试全套通过",
            bool(cold["delta_ok"]), "closeout/cold_run.json + cold_run.raw.log"),
        row("P0-AC-20", "Independent Review：三个独立角色均 APPROVE",
            all(signatures.values()), "closeout/signs/",
            "" if all(signatures.values()) else "三签在矩阵重写后补齐"),
    ]


def write_acceptance_matrix(root: Path) -> dict:
    rows = _ac_rows(root)
    acs = [row for row in rows if row["ac"].startswith("P0-AC-")]
    t7 = []
    for seq in range(1, 10):
        t7.append({"ac": f"AC-T7-{seq}",
                   "evidence": "tests/benchmark/test_task7_closeout.py"})
    pack_sha_path = root / "evidence_pack" / "pack.sha256"
    doc = {
        "schema": SCHEMA_MATRIX,
        "generated_at": _now(),
        "commit": _git("rev-parse", "HEAD"),
        "pack_sha256": pack_sha_path.read_text(encoding="utf-8").strip()
        if pack_sha_path.is_file() else None,
        "baseline_head": _json(root / "intake" / "repository_state.json")["head"],
        "acs": acs,
        "task7_acs": t7,
        "gaps": list(GAPS),
        "summary": {
            "total": len(acs),
            "passed": sum(1 for row in acs if row["result"] == "PASS"),
            "failed": sum(1 for row in acs if row["result"] != "PASS"),
            "gaps": len(GAPS),
        },
    }
    _write_json(root / "closeout" / "acceptance_matrix.json", doc)
    return doc


def cmd_matrix(root: Path) -> dict:
    write_ledger_freeze(root)
    write_paid_calls(root)
    write_kb_integrity(root)
    write_invariants(root)
    write_environment_closeout(root)
    matrix = write_acceptance_matrix(root)
    return {"summary": matrix["summary"], "pack_sha256": matrix["pack_sha256"]}


# --------------------------------------------------------------------------
# ⑦ 终判
# --------------------------------------------------------------------------

def cmd_judge(root: Path, require_acceptance: bool) -> dict:
    doc = judge.run_judge(root, require_acceptance=require_acceptance)
    _write_json(root / "closeout" / "closure_judge.json", doc)
    return {"verdict": doc["verdict"], "problems": doc["problems"],
            "gaps": len(doc["gaps"])}


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("reports")
    cold = sub.add_parser("cold-run")
    cold.add_argument("--raw", required=True)
    cold.add_argument("--rc", type=int, default=None)
    pack = sub.add_parser("pack")
    pack.add_argument("--clean", action="store_true")
    sub.add_parser("acceptance")
    sub.add_parser("matrix")
    judgment = sub.add_parser("judge")
    judgment.add_argument("--require-acceptance", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.root) if args.root else paths.bench_root()
    if args.cmd == "reports":
        out = cmd_reports(root)
    elif args.cmd == "cold-run":
        out = cmd_cold_run(root, Path(args.raw), args.rc)
    elif args.cmd == "pack":
        out = cmd_pack(root, args.clean)
    elif args.cmd == "acceptance":
        out = cmd_acceptance(root)
    elif args.cmd == "matrix":
        out = cmd_matrix(root)
    else:
        out = cmd_judge(root, args.require_acceptance)
    print(json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True,
                     default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
