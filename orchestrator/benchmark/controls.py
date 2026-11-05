"""Phase 2.8.0 Task 5：Golden / Negative Controls / Mutation 的机判运行器。

**不变量（T5.1）**：

1. 合成 golden 只验证「评测器会判过」，不冒充能力证据（真实参考在 Task 6）；
2. 任一负例被放过 ⇒ 评测器不可信 ⇒ ``all_passed=False``（调用方 rc≠0）；
3. 变异只能在副本上做（``mutation.apply``）；
4. D5-4：每条含 ``case_id/task_id/expected/actual/rejected/reason``；
5. D5-5：判负也算通过（``rejected=true``），不是 assert 崩。

CLI 薄封装见 ``scripts/benchmark/run_negative_controls.py``。
"""
from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from orchestrator.benchmark import evaluator as ev
from orchestrator.benchmark import mutation as mut
from orchestrator.benchmark import paths
from orchestrator.benchmark import result_schema as rs
from orchestrator.benchmark import task_spec as ts
from orchestrator.benchmark.registry import BenchmarkRegistry
from orchestrator.common.fsutil import ensure_private_dir

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "benchmark"
GOLDEN_CASES = FIXTURE_ROOT / "golden" / "cases.json"
NEGATIVE_CASES = FIXTURE_ROOT / "negative" / "cases.json"

GOLDEN_RESULTS = "golden_smoke.json"
NEGATIVE_RESULTS = "negative_control_results.json"
MUTATION_RESULTS = "mutation_results.json"

SCHEMA_GOLDEN = "simpraxis.benchmark_golden_smoke.v1"
SCHEMA_NEGATIVE = "simpraxis.benchmark_negative_controls.v1"
SCHEMA_MUTATION = "simpraxis.benchmark_mutation_results.v1"


class ControlError(RuntimeError):
    """夹具索引/环境不合法（基建问题，不是候选问题）。"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlError(f"读 {path} 失败：{exc}") from exc


def load_cases(path: "str | Path") -> "list[dict]":
    doc = _read_json(Path(path))
    cases = doc.get("cases") if isinstance(doc, dict) else None
    if not isinstance(cases, list) or not cases:
        raise ControlError(f"{path} 缺 cases 列表")
    for index, case in enumerate(cases):
        for key in ("case_id", "task_id", "dir"):
            if not isinstance(case.get(key), str) or not case[key]:
                raise ControlError(f"{path} 第 {index} 条缺 {key}")
    return cases


def _make_evaluator(spec: ts.TaskSpec) -> ev.Evaluator:
    """评测器构造点（AC-T5-6 的放水注入点，测试用 monkeypatch 替换）。"""
    return ev.Evaluator(spec=spec)


def evaluate_case(task_id: str, run_dir: Path, out_dir: Path, *,
                  benchmark_root: "Path | None" = None) -> dict:
    """按 case 跑一次评测；返回评测器写的 ``evaluation_result.json`` 内容。"""
    spec = BenchmarkRegistry().get(task_id)       # 判据根要在替换前加载
    previous = ts.BENCHMARK_ROOT
    try:
        if benchmark_root is not None:
            ts.BENCHMARK_ROOT = Path(benchmark_root)
        _make_evaluator(spec).evaluate(run_dir, out_dir)
    finally:
        ts.BENCHMARK_ROOT = previous
    result_path = Path(out_dir) / rs.EVALUATION_NAME
    if not result_path.is_file():
        return {"status": "MISSING", "hard_gates": []}
    return _read_json(result_path)


def _hit_map(doc: dict) -> "dict[str, str]":
    out: "dict[str, str]" = {}
    for hit in doc.get("hard_gates") or []:
        if isinstance(hit, dict) and isinstance(hit.get("id"), str):
            out[hit["id"]] = str(hit.get("detail") or "")
    return out


def _reason(hits: "dict[str, str]") -> str:
    return "; ".join(f"{gate}: {detail}" for gate, detail in sorted(hits.items()))


def judge_case(case: dict, doc: dict) -> dict:
    """把一条控制用例的期望与实际比对成结构化行（D5-4 / D5-5）。"""
    hits = _hit_map(doc)
    expected_gates = [str(gate) for gate in case.get("expected_gates") or []]
    expected_detail = str(case.get("expected_detail") or "")
    actual_status = str(doc.get("status") or "MISSING")
    gates_ok = set(expected_gates) <= set(hits)
    detail_ok = (not expected_detail) or any(
        expected_detail in hits.get(gate, "") for gate in expected_gates)
    rejected = actual_status != "PASS" and gates_ok and detail_ok
    return {
        "case_id": case["case_id"],
        "task_id": case["task_id"],
        "dir": case["dir"],
        "defect": case.get("defect"),
        "expected_status": case.get("expected_status"),
        "expected_gates": expected_gates,
        "expected_detail": expected_detail,
        "actual_status": actual_status,
        "actual_gates": sorted(hits),
        "rejected": bool(rejected),
        "gates_ok": bool(gates_ok),
        "detail_ok": bool(detail_ok),
        "reason": _reason(hits) or "评测器未命中任何硬门",
    }


def _golden_rows(cases: Iterable[dict], work: Path) -> "list[dict]":
    rows = []
    for case in cases:
        run_dir = FIXTURE_ROOT / "golden" / case["dir"]
        doc = evaluate_case(case["task_id"], run_dir, work / case["case_id"])
        hits = _hit_map(doc)
        rows.append({
            "case_id": case["case_id"],
            "task_id": case["task_id"],
            "dir": case["dir"],
            "actual_status": doc.get("status"),
            "weighted_score": doc.get("weighted_score"),
            "hard_gates": sorted(hits),
            "passed": doc.get("status") == "PASS" and not hits,
            "reason": _reason(hits) or "无硬门命中，全部维度满分",
        })
    return rows


def _negative_rows(cases: Iterable[dict], work: Path) -> "list[dict]":
    return [judge_case(case, evaluate_case(
        case["task_id"], FIXTURE_ROOT / "negative" / case["dir"],
        work / case["case_id"])) for case in cases]


def _mutation_rows(golden_dir: Path, work: Path) -> "list[dict]":
    rows = []
    for spec in mut.MUTATIONS:
        dest = work / spec.name
        mutated = mut.apply(spec.name, golden_dir, dest,
                            benchmark_root=ts.BENCHMARK_ROOT)
        case = {
            "case_id": f"mut_{spec.name}",
            "task_id": "mujoco_contact_push_v1",
            "dir": str(dest.relative_to(work)),
            "defect": spec.description,
            "expected_status": spec.expected_status,
            "expected_gates": spec.expected_gates,
            "expected_detail": spec.expected_detail,
        }
        row = judge_case(case, evaluate_case(
            case["task_id"], mutated.run_dir, work / f"{spec.name}_eval",
            benchmark_root=mutated.benchmark_root))
        row["mutation"] = spec.name
        row["status_ok"] = row["actual_status"] == spec.expected_status
        row["rejected"] = bool(row["rejected"] and row["status_ok"])
        row["notes"] = mutated.notes
        rows.append(row)
    return rows


@dataclass
class ControlReport:
    out_root: Path
    golden: dict = field(default_factory=dict)
    negatives: dict = field(default_factory=dict)
    mutations: dict = field(default_factory=dict)

    @property
    def all_passed(self) -> bool:
        return bool(self.golden.get("all_passed")
                    and self.negatives.get("all_passed")
                    and self.mutations.get("all_passed"))

    def summary(self) -> dict:
        return {
            "golden_passed": f"{sum(1 for r in self.golden.get('rows', []) if r['passed'])}"
                              f"/{len(self.golden.get('rows', []))}",
            "negative_rejected":
                f"{sum(1 for r in self.negatives.get('rows', []) if r['rejected'])}"
                f"/{len(self.negatives.get('rows', []))}",
            "mutation_rejected":
                f"{sum(1 for r in self.mutations.get('rows', []) if r['rejected'])}"
                f"/{len(self.mutations.get('rows', []))}",
            "all_passed": self.all_passed,
        }


def run_controls(*, out_root: "str | Path | None" = None,
                 tasks: "Iterable[str] | None" = None) -> ControlReport:
    """跑全套控制并落三份结果 JSON（T5.7）。``tasks`` 只影响负例筛选。"""
    target = ensure_private_dir(Path(out_root) if out_root
                                else paths.negative_controls_dir())
    golden_cases = load_cases(GOLDEN_CASES)
    negative_cases = load_cases(NEGATIVE_CASES)
    if tasks:
        wanted = set(tasks)
        golden_cases = [c for c in golden_cases if c["task_id"] in wanted]
        negative_cases = [c for c in negative_cases if c["task_id"] in wanted]

    report = ControlReport(out_root=target)
    with tempfile.TemporaryDirectory(prefix="task5_",
                                     dir=str(target)) as tmp:
        work = Path(tmp)
        golden_rows = _golden_rows(golden_cases, work)
        negative_rows = _negative_rows(negative_cases, work)
        if tasks:
            mutation_rows: "list[dict]" = []
        else:
            mutation_rows = _mutation_rows(
                FIXTURE_ROOT / "golden" / "mujoco", work)

    report.golden = {
        "schema": SCHEMA_GOLDEN,
        "generated_at": _now(),
        "count": len(golden_rows),
        "rows": golden_rows,
        "all_passed": all(row["passed"] for row in golden_rows),
    }
    report.negatives = {
        "schema": SCHEMA_NEGATIVE,
        "generated_at": _now(),
        "count": len(negative_rows),
        "rejected": sum(1 for row in negative_rows if row["rejected"]),
        "rows": negative_rows,
        "all_passed": all(row["rejected"] for row in negative_rows),
    }
    report.mutations = {
        "schema": SCHEMA_MUTATION,
        "generated_at": _now(),
        "count": len(mutation_rows),
        "rejected": sum(1 for row in mutation_rows if row["rejected"]),
        "rows": mutation_rows,
        "all_passed": (all(row["rejected"] for row in mutation_rows)
                       if mutation_rows else True),
    }
    for name, doc in ((GOLDEN_RESULTS, report.golden),
                      (NEGATIVE_RESULTS, report.negatives),
                      (MUTATION_RESULTS, report.mutations)):
        (target / name).write_text(
            json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
    return report


def clean_workdirs(out_root: Path) -> None:
    """清理上次中断留下的临时工作目录（只删 ``task5_`` 前缀）。"""
    for entry in sorted(Path(out_root).glob("task5_*")):
        if entry.is_dir():
            shutil.rmtree(entry)
