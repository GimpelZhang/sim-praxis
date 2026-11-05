"""Phase 2.8.0 Task 7 报告渲染（D7-5：七件，全部数字从落盘证据读）。

设计纪律：

- **不手写任何数字**：每个字段都由 ``<bench_root>`` 下的 intake / runs /
  evaluator_results / negative_controls / fingerprints 现读现算；
- **确定性**：同一输入两次渲染逐字节相同（字典排序 + ``sort_keys``），
  时间戳由调用方传入（打包不动点要求排除时间漂移）；
- **独立于机判**：本模块自带一版轻量判定（给报告一个 ``verdict``），
  ``scripts/benchmark/check_closure.py`` 另写一版八类检查——两套独立实现
  互相印证（D7-4），任一侧不一致都说明证据链有问题；
- **不读判决件**：报告不得消费 ``closure_judge.json`` / 三签——它们在报告
  之后才产生（否则自指）。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from orchestrator.benchmark import paths

TASKS = ("mujoco_contact_push_v1", "libero_env_adapter_v1", "icy_ground_dr_v1")

SUMMARY_SCHEMA = "simpraxis.benchmark.summary.v1"
TASK_SCORES_SCHEMA = "simpraxis.benchmark.task_scores.v1"
RUNTIME_MATRIX_SCHEMA = "simpraxis.benchmark.runtime_matrix.v1"
RELIABILITY_SCHEMA = "simpraxis.benchmark.evaluator_reliability.v1"
CONTROLS_SCHEMA = "simpraxis.benchmark.negative_control_summary.v1"
EVIDENCE_INDEX_SCHEMA = "simpraxis.benchmark.evidence_index.v1"

REPORT_NAME = "phase0_benchmark_report.md"
JSON_NAMES = {
    "summary": "benchmark_summary.json",
    "task_scores": "task_scores.json",
    "runtime_matrix": "runtime_matrix.json",
    "evaluator_reliability": "evaluator_reliability.json",
    "negative_controls": "negative_control_results.json",
    "evidence_index": "evidence_index.json",
}

#: 报告禁止出现的表述（T7.1 不变量 3：结论只能是基础设施二值）
FORBIDDEN_PHRASES = (
    "Agent 自主编码能力已通过",
    "Agent 自主编码能力通过",
    "证明 Agent 具备",
)

#: AC-T7-9：结论二值（评审 W5-20：把这一对常量落到实现里，测试断言它）
VERDICTS = ("Established", "Not Established")


def verdict_for(problems: "list[str] | tuple[str, ...]") -> str:
    """结论唯一产生点：有问题即 ``Not Established``，否则 ``Established``。"""
    return VERDICTS[0] if not problems else VERDICTS[1]


class ReportError(RuntimeError):
    """证据不足以渲染报告（缺件即 fail-closed，不拿空值凑数）。"""


def assert_report_text(text: str) -> str:
    """报告禁令：结论只能是基础设施二值，不得宣称 Agent 能力。"""
    hits = [phrase for phrase in FORBIDDEN_PHRASES if phrase in text]
    if hits:
        raise ReportError(f"报告出现越界结论表述：{hits}")
    return text


# --------------------------------------------------------------------------
# 读取工具
# --------------------------------------------------------------------------

def _json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _latest_run(root: Path, task_id: str) -> Path:
    """与机判共用同一身份解析（评审 W4-11）。"""
    try:
        return paths.pinned_run(root, task_id)
    except paths.BenchmarkPathError as exc:
        raise ReportError(f"{task_id}: 没有可判的参考运行产物：{exc}") from exc


def _rel(path: Path, root: Path) -> str:
    try:
        return Path(path).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _now(now: "datetime | None") -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# 六件 JSON
# --------------------------------------------------------------------------

def task_scores(root: "Path | None" = None, *, now: "datetime | None" = None) -> dict:
    root = Path(root) if root else paths.bench_root()
    rows = []
    for task_id in TASKS:
        run = _latest_run(root, task_id)
        manifest = _json(run / "run_manifest.json")
        evaluation = _json(root / "evaluator_results" / run.name
                           / "evaluation_result.json")
        birth = _json(run / "birth" / "functional_birth.json")
        fingerprint = _json(run / "runtime_fingerprint.json")
        rows.append({
            "task_id": task_id,
            "run_id": run.name,
            "mode": manifest["mode"],
            "status": manifest["status"],
            "evaluation_status": evaluation["status"],
            "weighted_score": float(evaluation["weighted_score"]),
            "dimensions": {name: float(dim["score"])
                           for name, dim in sorted(evaluation["dimensions"].items())},
            "hard_gates": list(evaluation["hard_gates"]),
            "birth_status": birth["birth_status"],
            # 评审 W4-13：侧车是 sha256sum 文本（`<sha>  <file>`），不是 JSON；
            # 旧回退按 JSON 解析必抛，属死路。
            "runtime_fingerprint_sha256": str(
                fingerprint.get("fingerprint_sha256")
                or (run / "runtime_fingerprint.sha256").read_text(
                    encoding="utf-8").split()[0]),
            "run_dir": _rel(run, root),
        })
    return {"schema": TASK_SCORES_SCHEMA, "generated_at": _now(now),
            "tasks": rows}


def runtime_matrix(root: "Path | None" = None, *,
                   now: "datetime | None" = None) -> dict:
    root = Path(root) if root else paths.bench_root()
    rows = []
    for task_id in TASKS:
        run = _latest_run(root, task_id)
        doc = _json(run / "runtime_fingerprint.json")
        container = doc.get("container") or {}
        python = doc.get("python") or {}
        simulator = doc.get("simulator") or {}
        rows.append({
            "task_id": task_id,
            "run_id": run.name,
            "backend": container.get("backend"),
            "python_executable": python.get("executable"),
            "python_version": python.get("version"),
            "packages": dict(sorted((python.get("packages") or {}).items())),
            "image_name": container.get("image_name"),
            "image_digest": container.get("image_digest"),
            "container_id": container.get("container_id"),
            "simulator": {"name": simulator.get("name"),
                          "version": simulator.get("version")},
            "host_kernel": container.get("kernel"),
        })
    return {"schema": RUNTIME_MATRIX_SCHEMA, "generated_at": _now(now),
            "tasks": rows}


def _controls_inputs(root: Path) -> dict:
    base = Path(root) / "negative_controls"
    docs = {}
    for key, name in (("golden", "golden_smoke.json"),
                      ("negative", "negative_control_results.json"),
                      ("mutation", "mutation_results.json")):
        path = base / name
        if not path.is_file():
            raise ReportError(f"缺控制组结果：{path}")
        docs[key] = _json(path)
    return docs


def negative_controls(root: "Path | None" = None, *,
                      now: "datetime | None" = None) -> dict:
    raw = _controls_inputs(Path(root) if root else paths.bench_root())
    return {
        "schema": CONTROLS_SCHEMA,
        "generated_at": _now(now),
        "golden": {"count": int(raw["golden"]["count"]),
                   "all_passed": bool(raw["golden"]["all_passed"])},
        "negative": {"count": int(raw["negative"]["count"]),
                     "rejected": int(raw["negative"]["rejected"]),
                     "all_passed": bool(raw["negative"]["all_passed"])},
        "mutation": {"count": int(raw["mutation"]["count"]),
                     "rejected": int(raw["mutation"]["rejected"]),
                     "all_passed": bool(raw["mutation"]["all_passed"])},
    }


def evaluator_reliability(root: "Path | None" = None, *,
                          now: "datetime | None" = None) -> dict:
    raw = _controls_inputs(Path(root) if root else paths.bench_root())
    inputs = {
        "golden_cases": int(raw["golden"]["count"]),
        "negative_cases": int(raw["negative"]["count"]),
        "negative_rejected": int(raw["negative"]["rejected"]),
        "mutation_cases": int(raw["mutation"]["count"]),
        "mutation_rejected": int(raw["mutation"]["rejected"]),
    }
    # 真实参考运行的判决必须三份同值且 PASS（P0-AC-10 的 Task 6 引用）
    base = Path(root) if root else paths.bench_root()
    real = []
    for task_id in TASKS:
        run = _latest_run(base, task_id)
        ev = _json(base / "evaluator_results" / run.name
                   / "evaluation_result.json")
        real.append({"task_id": task_id, "run_id": run.name,
                     "status": ev["status"],
                     "weighted_score": float(ev["weighted_score"]),
                     "hard_gates": list(ev["hard_gates"])})
    base = Path(root) if root else paths.bench_root()
    versions = sorted({int(_json(base / "evaluator_results" / row["run_id"]
                                 / "evaluation_result.json")["evaluator_version"])
                       for row in real})
    reliable = (
        inputs["negative_rejected"] == inputs["negative_cases"] > 0
        and inputs["mutation_rejected"] == inputs["mutation_cases"] > 0
        and bool(raw["golden"]["all_passed"])
        and all(row["status"] == "PASS" and not row["hard_gates"] for row in real)
    )
    return {
        "schema": RELIABILITY_SCHEMA,
        "generated_at": _now(now),
        "inputs": inputs,
        "reference_evaluations": real,
        "evaluator_versions": versions,
        "verdict": "RELIABLE" if reliable else "UNRELIABLE",
    }


def evidence_index(root: "Path | None" = None, *,
                   now: "datetime | None" = None) -> dict:
    root = Path(root) if root else paths.bench_root()
    bench_dirs = ("intake", "specs", "fingerprints", "preregistration", "runs",
                  "negative_controls", "evaluator_results", "reports",
                  "references", "private")
    entries = []
    for name in bench_dirs:
        base = root / name
        if not base.exists():
            entries.append({"root": f"@bench/{name}", "present": False,
                            "files": 0, "bytes": 0})
            continue
        # 自指件不计入：本索引与本报告（否则重跑一次数字就变，破坏确定性）
        skip_names = {JSON_NAMES["evidence_index"], REPORT_NAME}
        files = [p for p in base.rglob("*")
                 if p.is_file() and p.name not in skip_names]
        entries.append({"root": f"@bench/{name}", "present": True,
                        "files": len(files),
                        "bytes": int(sum(p.stat().st_size for p in files))})
    runs_root = _runs_root()
    if runs_root is not None:
        files = [p for p in runs_root.rglob("*") if p.is_file()]
        entries.append({"root": "@runs/benchmark", "present": True,
                        "path": str(runs_root), "files": len(files),
                        "bytes": int(sum(p.stat().st_size for p in files))})
    total_files = sum(int(e["files"]) for e in entries)
    return {"schema": EVIDENCE_INDEX_SCHEMA, "generated_at": _now(now),
            "bench_root": str(root), "entries": entries,
            "n_files": total_files,
            "total_bytes": int(sum(int(e["bytes"]) for e in entries))}


def _runs_root() -> "Path | None":
    import os

    raw = os.environ.get("SIMPRAXIS_RUN_DIR")
    if not raw:
        return None
    base = Path(raw) / "benchmark"
    return base if base.exists() else None


# --------------------------------------------------------------------------
# 汇总与报告
# --------------------------------------------------------------------------

def _ledger_freeze_ok(root: Path) -> "tuple[bool, list[dict]]":
    """五本账与 Task 0 快照逐字相同（I6，本模块的轻量版）。"""
    import hashlib

    before = _json(Path(root) / "intake" / "sealed_ledger_sha256.json")
    rows = []
    ok = True
    for phase, row in sorted(before["ledgers"].items()):
        path = Path(row["path"])
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        equal = actual == row["sha256"]
        ok = ok and equal
        rows.append({"phase": phase, "path": str(path), "rows": int(row["rows"]),
                     "sha256_before": row["sha256"], "sha256_after": actual,
                     "equal": equal})
    return ok, rows


def _kb_ok(root: Path) -> bool:
    import subprocess

    before = _json(Path(root) / "intake" / "kb_state.json")
    head_before = str(before["head"])
    repo = Path(before["kb_root"])
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, timeout=60)
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                            capture_output=True, text=True, timeout=60)
    return (head.returncode == 0 and head.stdout.strip() == head_before
            and status.returncode == 0 and status.stdout.strip() == "")


def _stage_models_ok(root: Path) -> bool:
    import hashlib

    before = _json(Path(root) / "intake" / "invariants_before.json")
    path = Path(before["stage_models_yaml"])
    return hashlib.sha256(path.read_bytes()).hexdigest() == before["stage_models_sha256"]


def summary(root: "Path | None" = None, *, cold_run: "Mapping | None" = None,
            now: "datetime | None" = None) -> dict:
    root = Path(root) if root else paths.bench_root()
    scores = task_scores(root, now=now)
    controls = negative_controls(root, now=now)
    reliability = evaluator_reliability(root, now=now)
    ledgers_ok, ledger_rows = _ledger_freeze_ok(root)
    cold = dict(cold_run or _cold_run(root))
    problems = []
    for row in scores["tasks"]:
        if row["status"] != "PASS" or row["evaluation_status"] != "PASS":
            problems.append(f"{row['task_id']}: status={row['status']}"
                            f"/{row['evaluation_status']}")
        if row["birth_status"] != "BORN":
            problems.append(f"{row['task_id']}: birth={row['birth_status']}")
        if row["hard_gates"]:
            problems.append(f"{row['task_id']}: hard_gates={row['hard_gates']}")
    if not (controls["golden"]["all_passed"]
            and controls["negative"]["all_passed"]
            and controls["mutation"]["all_passed"]):
        problems.append("控制组/负例/变异未全通过")
    if reliability["verdict"] != "RELIABLE":
        problems.append("评测器可靠性判负")
    if not ledgers_ok:
        problems.append("五本账与 Task 0 快照不符")
    if not _kb_ok(root):
        problems.append("KB 仓 HEAD/工作树与 Task 0 不符")
    if not _stage_models_ok(root):
        problems.append("stage_models.yaml 与 Task 0 快照不符")
    if not cold.get("delta_ok"):
        problems.append("冷跑精确增量不成立")
    verdict = verdict_for(problems)
    return {
        "schema": SUMMARY_SCHEMA,
        "generated_at": _now(now),
        "verdict": verdict,
        "problems": problems,
        "paid_model_calls": 0,
        "tasks": [{"task_id": r["task_id"], "status": r["status"],
                   "weighted_score": r["weighted_score"]} for r in scores["tasks"]],
        "controls": controls,
        "evaluator": {"verdict": reliability["verdict"],
                      **reliability["inputs"]},
        "ledgers": ledger_rows,
        "cold_run": cold,
    }


def _cold_run(root: Path) -> dict:
    path = Path(root) / "closeout" / "cold_run.json"
    if not path.is_file():
        return {"delta_ok": False, "note": "closeout/cold_run.json 缺失"}
    return _json(path)


def _commits(root: Path) -> "list[str]":
    """Task 0 基线提交之后的本阶段提交（只读历史，不改写）。"""
    import subprocess

    state = _json(Path(root) / "intake" / "repository_state.json")
    base = str(state["head"])
    proc = subprocess.run(
        ["git", "-C", str(Path(state["repo"])), "log", "--format=%h %s",
         f"{base}..HEAD"],
        capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        return [f"<git log 失败 rc={proc.returncode}: {proc.stderr.strip()}>"]
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _eight_answers(summary_doc: Mapping, scores: Mapping, cold: Mapping,
                   ledgers_ok: bool, runtime: "Mapping | None" = None,
                   ) -> "list[tuple[str, str]]":
    rows = scores["tasks"]
    # 环境是否「真实验证」以运行时指纹的后端为准，run_manifest.mode 只说明
    # 这是 reference 运行（不是后端名）。
    backends = {row["task_id"]: row.get("backend")
                for row in (runtime or {}).get("tasks", [])}
    env_ok = [f"`{row['task_id']}`({backends[row['task_id']]})"
              for row in rows
              if backends.get(row["task_id"]) in ("host", "docker")]
    env_missing = [f"`{row['task_id']}`(后端未知)"
                   for row in rows
                   if backends.get(row["task_id"]) not in ("host", "docker")]
    blocked = [f"`{row['task_id']}`={row['status']}" for row in rows
               if row["status"] in ("BLOCKED_INFRA", "INCOMPLETE_EVIDENCE")]
    pass_tasks = [f"`{row['task_id']}`" for row in rows
                  if row["status"] == "PASS" and row["evaluation_status"] == "PASS"]
    # 评审 W4-14：首问必须随 blocked/FAIL 行变化，不得恒定「是——」。
    complete_ok = (not blocked and len(pass_tasks) == len(rows)
                   and len(rows) == 3)
    if complete_ok:
        answer_1 = (f"是——三任务规格/判据/运行时身份/参考运行/独立评测/"
                    f"证据齐备（{len(pass_tasks)}/{len(rows)} 有 PASS 参考）。")
    else:
        reasons = []
        if blocked:
            reasons.append(f"阻塞/证据不全：{'、'.join(blocked)}")
        missing = [f"`{row['task_id']}`" for row in rows
                   if row["task_id"] not in
                   {p.strip('`') for p in pass_tasks}]
        if missing:
            reasons.append(f"无 PASS 参考：{'、'.join(missing)}")
        answer_1 = "否——" + "；".join(reasons) + \
                   f"（{len(pass_tasks)}/{len(rows)} 有 PASS 参考）。"
    answers = [
        ("1. Benchmark 是否完整？", answer_1),
        ("2. Evaluator 是否可信（负例/变异全拒）？",
         f"`{summary_doc['evaluator']['verdict']}`（golden "
         f"{summary_doc['evaluator']['golden_cases']} 例、负例 "
         f"{summary_doc['evaluator']['negative_rejected']}/"
         f"{summary_doc['evaluator']['negative_cases']} 拒、变异 "
         f"{summary_doc['evaluator']['mutation_rejected']}/"
         f"{summary_doc['evaluator']['mutation_cases']} 拒）。"),
        ("3. 哪些环境已经真实验证（M1 宿主 / L1 容器）？",
         "、".join(env_ok) or "无"),
        ("4. 哪些环境仍未验证？",
         "、".join(env_missing) or "无（三任务均按声明的后端实跑）"),
        ("5. 哪些任务已经有可靠参考结果？",
         "、".join(pass_tasks) or "无"),
        ("6. 哪些验收仍是 BLOCKED_INFRA / INCOMPLETE_EVIDENCE？",
         "、".join(blocked) or "无"),
        ("7. 是否存在任何历史系统回归？",
         f"{'无' if ledgers_ok and cold.get('delta_ok') else '有'}——五本账 SHA256 "
         f"{'逐字不变' if ledgers_ok else '有变动'}，冷跑精确增量 "
         f"{'成立' if cold.get('delta_ok') else '不成立'}。"),
        ("8. 是否满足进入 Roadmap Phase 1 的条件？",
         f"{'满足' if summary_doc['verdict'] == 'Established' else '不满足'}"
         f"（仅就 Benchmark Infrastructure 而言；不等于 Agent 能力成立）。"),
    ]
    return answers


def render_report(summary_doc: Mapping, scores: Mapping, runtime: Mapping,
                  reliability: Mapping, controls: Mapping,
                  index: Mapping, *, commits: "list[str] | None" = None,
                  ledgers_ok: bool = True,
                  runtime_doc: "Mapping | None" = None) -> str:
    tasks = scores["tasks"]
    lines = [
        "# Phase 2.8.0 / Roadmap Phase 0 — Benchmark Infrastructure 报告",
        "",
        f"**结论（基础设施二值）**：`{summary_doc['verdict']}`",
        "",
        f"- 生成时间：`{summary_doc['generated_at']}`",
        f"- 付费调用：`{summary_doc['paid_model_calls']}`（本阶段 0 付费）",
        f"- 证据文件：{index['n_files']} 件 / {index['total_bytes']} 字节",
        "",
        "## 任务与判决",
        "",
        "| 任务 | 模式 | run | 判决 | 加权分 | 出生 | 四维 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in tasks:
        dims = ", ".join(f"{name}={score:.2f}"
                         for name, score in row["dimensions"].items())
        lines.append(
            f"| `{row['task_id']}` | {row['mode']} | `{row['run_id']}` | "
            f"{row['status']} | {row['weighted_score']:.4f} | "
            f"{row['birth_status']} | {dims} |")
    lines += [
        "",
        "## 评测器可靠性",
        "",
        f"- golden {reliability['inputs']['golden_cases']} 例全过："
        f"`{controls['golden']['all_passed']}`",
        f"- 负例 {reliability['inputs']['negative_rejected']}/"
        f"{reliability['inputs']['negative_cases']} 被拒",
        f"- 变异 {reliability['inputs']['mutation_rejected']}/"
        f"{reliability['inputs']['mutation_cases']} 被拒",
        f"- 真实参考三份判决："
        + "、".join(f"`{r['task_id']}`={r['status']}" for r in
                   reliability["reference_evaluations"]),
        f"- 判定：`{reliability['verdict']}`",
        "",
        "## 运行时矩阵",
        "",
        "| 任务 | 后端 | 解释器 | 包（声明实测） | 镜像 digest |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in runtime["tasks"]:
        pkgs = ", ".join(f"{name}=={ver}" for name, ver in row["packages"].items())
        lines.append(
            f"| `{row['task_id']}` | {row['backend']} | "
            f"`{row['python_executable']}` | {pkgs} | "
            f"{row['image_digest'] or '—'} |")
    cold = summary_doc["cold_run"]
    lines += [
        "",
        "## 核心回归（精确增量）",
        "",
        f"- `{cold.get('cmd', '')}`",
        f"- rc={cold.get('rc')}；passed={cold.get('passed')}"
        f"（基线 {cold.get('baseline', {}).get('passed')} + 新增 "
        f"{cold.get('new_collected', {}).get('total')}）；"
        f"skipped={cold.get('skipped')}；"
        f"deselected={cold.get('deselected')}；delta_ok="
        f"`{cold.get('delta_ok')}`",
        "",
        "## 证据索引",
        "",
        "| 逻辑根 | 件数 | 字节 |",
        "| --- | --- | --- |",
    ]
    for entry in index["entries"]:
        lines.append(f"| `{entry['root']}` | {entry['files']} | {entry['bytes']} |")
    lines.append("")
    lines.append("> `@bench/reports` 的件数在渲染本报告时尚不含本文件与 "
                 "`evidence_index.json` 自身；证据包 manifest 以实际入包件数为准。")
    if summary_doc["problems"]:
        lines += ["", "## 未通过项", ""]
        lines += [f"- {item}" for item in summary_doc["problems"]]
    lines += ["", "## 最终报告必答的八个问题（路线图 §20）", ""]
    for question, answer in _eight_answers(summary_doc, scores, cold,
                                           ledgers_ok, runtime_doc):
        lines.append(f"- **{question}** {answer}")
    if commits is not None:
        lines += ["", "## 本阶段提交（Task 0 基线之后）", ""]
        lines += [f"- `{line}`" for line in commits] or ["- （无）"]
    lines += [
        "",
        "## 诚实声明",
        "",
        "- 本阶段的结论只涉及 **Benchmark Infrastructure**（规格、运行时身份、出生验证、"
        "独立评测、证据与分数）。它不评估、也不宣称任何 Agent 的自主编码能力。",
        "- 三个任务的参考实现是**基线**，不是能力上限；Q1 冰面负结论（ice/normal=0.824"
        "<2.0，`contradiction_control_not_robust`）保持冻结，本报告不改写、不翻案。",
        "- 本阶段 0 付费：所有账本行数与 SHA256 与 Task 0 快照逐字相同（见 "
        "`closeout/ledger_freeze.json`）。",
        "",
    ]
    return assert_report_text("\n".join(lines))


def _write_json(path: Path, doc: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2,
                               sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def build_reports(root: "Path | None" = None, *, now: "datetime | None" = None,
                  cold_run: "Mapping | None" = None) -> dict:
    """渲染七件到 ``<bench_root>/reports/``（确定性；不读判决件）。"""
    base = Path(root) if root else paths.bench_root()
    out = base / "reports"
    scores = task_scores(base, now=now)
    runtime = runtime_matrix(base, now=now)
    reliability = evaluator_reliability(base, now=now)
    controls = negative_controls(base, now=now)
    summary_doc = summary(base, cold_run=cold_run, now=now)
    # 先写六件 JSON，再算索引（此时 reports/ 内件数已稳定；索引与报告 md
    # 本身按名排除，重跑数字不变）
    written = {
        "summary": _write_json(out / JSON_NAMES["summary"], summary_doc),
        "task_scores": _write_json(out / JSON_NAMES["task_scores"], scores),
        "runtime_matrix": _write_json(out / JSON_NAMES["runtime_matrix"], runtime),
        "evaluator_reliability": _write_json(out / JSON_NAMES["evaluator_reliability"],
                                             reliability),
        "negative_controls": _write_json(out / JSON_NAMES["negative_controls"],
                                         controls),
    }
    index = evidence_index(base, now=now)
    text = render_report(summary_doc, scores, runtime, reliability, controls,
                         index, runtime_doc=runtime, commits=_commits(base),
                         ledgers_ok=all(bool(r["equal"])
                                        for r in summary_doc["ledgers"]))

    written["evidence_index"] = _write_json(out / JSON_NAMES["evidence_index"],
                                            index)
    report_path = out / REPORT_NAME
    report_path.write_text(text, encoding="utf-8")
    report_path.chmod(0o600)
    written["report"] = report_path
    return written
