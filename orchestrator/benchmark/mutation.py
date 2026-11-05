"""Phase 2.8.0 Task 5：十类评测器变异（路线图 §9.4 / D5-3）。

**纪律**：

1. 原件只读：每个变异器先把 golden 目录整体复制到调用方给的临时目录，
   所有篡改只发生在副本上（D5-3）；
2. 变异必须**定向**：篡改后要 ``reseal()`` 重算副本自己的
   ``run_manifest.json`` 绑定（fingerprint/birth/evidence 的 sha 与
   ``candidate_artifact_sha256``），让评测器**只能**因为被注入的缺陷而拒绝，
   而不是因为「digest 过期」这种无信息量的副作用；
3. 每类变异声明预期拒绝状态与命中门；``change_threshold`` 还要返回
   判据根副本（判据锁重算走 ``criteria_integrity``，需调用方把
   ``task_spec.BENCHMARK_ROOT`` 指过去）。
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.benchmark import runtime_fingerprint
from orchestrator.benchmark import task_spec as ts
from orchestrator.benchmark.result_schema import candidate_digest

RUN_MANIFEST = "run_manifest.json"
FINGERPRINT = "runtime_fingerprint.json"
BIRTH = "birth/functional_birth.json"
EVIDENCE_MANIFEST = "evidence/manifest.json"


class MutationError(RuntimeError):
    """变异前提不成立（golden 结构不符）；这是测试基建问题，不是候选问题。"""


@dataclass(frozen=True)
class Mutation:
    name: str
    expected_status: str
    expected_gates: "tuple[str, ...]"
    expected_detail: str
    description: str


@dataclass
class Mutated:
    """一次变异的结果：运行目录副本（+ 可选判据根副本）。"""

    run_dir: Path
    benchmark_root: "Path | None" = None
    notes: str = ""


MUTATIONS: "tuple[Mutation, ...]" = (
    Mutation("mutate_metrics", "FAIL", ("HG-M1-7",), "endpoint_distance_m",
             "metrics.json 自报值与 telemetry 重算不一致"),
    Mutation("mutate_evidence_manifest", "INVALID_ARTIFACT", ("HG-EVAL-6",),
             "evidence_manifest_sha256",
             "证据清单被改而 run_manifest 绑定未更新"),
    Mutation("mutate_run_manifest", "INVALID_ARTIFACT", ("HG-EVAL-6",),
             "未知 run mode",
             "run_manifest.mode 改成非枚举值"),
    Mutation("replace_fingerprint", "INVALID_RUNTIME", ("HG-EVAL-1",),
             "运行时不符",
             "换成另一环境的 runtime_fingerprint.json（内部自洽）"),
    Mutation("delete_logs", "INCOMPLETE_EVIDENCE", ("HG-EVAL-5",),
             "telemetry.jsonl",
             "删除 telemetry.jsonl（最关键的证据件）"),
    Mutation("bump_task_version", "INVALID_ARTIFACT", ("HG-EVAL-6",),
             "task_version",
             "run_manifest.task_version 与 task.yaml 不符"),
    Mutation("bump_evaluator_version", "INVALID_ARTIFACT", ("HG-EVAL-6",),
             "evaluator_version",
             "run_manifest.evaluator_version 与评测器不符"),
    Mutation("change_threshold", "BLOCKED_INFRA", ("HG-EVAL-7",),
             "criteria_lock_sha256",
             "改判据阈值但不重 LOCK（判据锁复算失败）"),
    Mutation("inject_judge_report", "INVALID_ARTIFACT", ("HG-EVAL-6",),
             "伪造判决件",
             "候选目录里塞 judge_report.json"),
    Mutation("forge_pass_file", "INVALID_ARTIFACT", ("HG-EVAL-6",),
             "伪造判决件",
             "候选目录里塞 PASS.txt / evaluation_result.json"),
)

MUTATION_NAMES = tuple(item.name for item in MUTATIONS)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2,
                               sort_keys=True) + "\n", encoding="utf-8")


def reseal(run_dir: Path, *, task_id: "str | None" = None) -> dict:
    """按副本现状重算 run_manifest 的派生绑定（不动身份/语义字段）。"""
    manifest_path = run_dir / RUN_MANIFEST
    if not manifest_path.is_file():
        raise MutationError(f"副本缺 {RUN_MANIFEST}：{run_dir}")
    manifest = _read_json(manifest_path)
    tid = task_id or manifest.get("task_id")
    from orchestrator.benchmark.registry import BenchmarkRegistry
    spec = BenchmarkRegistry().get(str(tid))

    for rel, key in ((FINGERPRINT, "runtime_fingerprint_sha256"),
                     (BIRTH, "birth_result_sha256"),
                     (EVIDENCE_MANIFEST, "evidence_manifest_sha256")):
        path = run_dir / rel
        manifest[key] = (ts.sha256_file(path) if path.is_file() else None)

    digests = {rel: ts.sha256_file(run_dir / rel)
               for rel in spec.outputs
               if rel != RUN_MANIFEST and (run_dir / rel).is_file()}
    manifest["candidate_artifact_sha256"] = candidate_digest(digests)
    _write_json(manifest_path, manifest)
    return manifest


def _copy(golden_dir: Path, dest: Path) -> Path:
    if not golden_dir.is_dir():
        raise MutationError(f"golden 目录不存在：{golden_dir}")
    shutil.copytree(golden_dir, dest)
    return dest


# --------------------------------------------------------------------------
# 十个变异器（每个都只改副本）
# --------------------------------------------------------------------------

def mutate_metrics(run_dir: Path) -> None:
    path = run_dir / "metrics.json"
    if not path.is_file():
        raise MutationError("golden 缺 metrics.json")
    doc = _read_json(path)
    doc["endpoint_distance_m"] = 0.0          # 与 telemetry 重算必然不符
    _write_json(path, doc)


def mutate_evidence_manifest(run_dir: Path) -> None:
    path = run_dir / EVIDENCE_MANIFEST
    if not path.is_file():
        raise MutationError("golden 缺 evidence/manifest.json")
    doc = _read_json(path)
    doc["files"] = [{"rel": "run_manifest.json", "sha256": "0" * 64}]
    _write_json(path, doc)                     # 不 reseal：绑定必须失配


def mutate_run_manifest(run_dir: Path) -> None:
    path = run_dir / RUN_MANIFEST
    doc = _read_json(path)
    doc["mode"] = "smoke"                      # 非枚举值
    _write_json(path, doc)


def replace_fingerprint(run_dir: Path) -> None:
    path = run_dir / FINGERPRINT
    if not path.is_file():
        raise MutationError("golden 缺 runtime_fingerprint.json")
    doc = _read_json(path)
    packages = dict((doc.get("python") or {}).get("packages") or {})
    packages["mujoco"] = "2.3.7"               # 宿主 3.15.0 ↔ 容器 2.3.7 事故面
    doc.setdefault("python", {})["packages"] = packages
    doc["fingerprint_sha256"] = runtime_fingerprint.fingerprint_sha256(doc)
    _write_json(path, doc)                     # 副本内自洽，只与 task.yaml 不符


def delete_logs(run_dir: Path) -> None:
    path = run_dir / "telemetry.jsonl"
    if not path.is_file():
        raise MutationError("golden 缺 telemetry.jsonl")
    path.unlink()


def bump_task_version(run_dir: Path) -> None:
    path = run_dir / RUN_MANIFEST
    doc = _read_json(path)
    doc["task_version"] = int(doc.get("task_version") or 1) + 1
    _write_json(path, doc)


def bump_evaluator_version(run_dir: Path) -> None:
    path = run_dir / RUN_MANIFEST
    doc = _read_json(path)
    doc["evaluator_version"] = int(doc.get("evaluator_version") or 1) + 1
    _write_json(path, doc)


def inject_judge_report(run_dir: Path) -> None:
    _write_json(run_dir / "judge_report.json", {
        "status": "PASS", "reason": "candidate wrote its own verdict"})


def forge_pass_file(run_dir: Path) -> None:
    (run_dir / "PASS.txt").write_text("PASS\n", encoding="utf-8")
    _write_json(run_dir / "evaluation_result.json", {
        "status": "PASS", "weighted_score": 1.0, "hard_gates": []})


#: change_threshold 在判据根副本里改的阈值（只改值，不重 LOCK）
_THRESHOLD_TO_TIGHTEN = "max_penetration_m"


def _mutate_root_copy(bench_root: Path, dest: Path) -> Path:
    """复制判据根并在副本里改阈值；task.yaml 的 lock 保持原样（故意失配）。"""
    shutil.copytree(bench_root, dest)
    changed = 0
    for path in sorted((dest / "criteria").glob("*.criteria.json")):
        doc = _read_json(path)
        entry = (doc.get("thresholds") or {}).get(_THRESHOLD_TO_TIGHTEN)
        if not isinstance(entry, dict):
            continue
        entry["value"] = float(entry["value"]) * 0.5   # 收紧一半
        _write_json(path, doc)
        changed += 1
    if not changed:
        raise MutationError(f"判据根副本里没有任何 {_THRESHOLD_TO_TIGHTEN} 可改")
    return dest


#: 这些变异**故意不 reseal**：它们的缺陷就是「绑定与实测不一致」
_NO_RESEAL = frozenset({"mutate_evidence_manifest"})


def apply(name: str, golden_dir: "str | Path", dest: "str | Path", *,
          benchmark_root: "str | Path | None" = None) -> Mutated:
    """把 ``name`` 变异施加到 golden 的副本上，返回副本路径。"""
    if name not in MUTATION_NAMES:
        raise MutationError(f"未知变异：{name!r}（可选 {MUTATION_NAMES}）")
    run_copy = _copy(Path(golden_dir), Path(dest))
    if name == "change_threshold":
        if benchmark_root is None:
            raise MutationError("change_threshold 需要 benchmark_root")
        mutated = Mutated(run_dir=run_copy,
                          benchmark_root=_mutate_root_copy(
                              Path(benchmark_root), Path(dest).with_name(
                                  Path(dest).name + "_bench_root")),
                          notes="判据根副本；task.yaml 的 criteria_lock 故意失配")
        reseal(run_copy)
        return mutated

    {
        "mutate_metrics": mutate_metrics,
        "mutate_evidence_manifest": mutate_evidence_manifest,
        "mutate_run_manifest": mutate_run_manifest,
        "replace_fingerprint": replace_fingerprint,
        "delete_logs": delete_logs,
        "bump_task_version": bump_task_version,
        "bump_evaluator_version": bump_evaluator_version,
        "inject_judge_report": inject_judge_report,
        "forge_pass_file": forge_pass_file,
    }[name](run_copy)
    if name not in _NO_RESEAL:
        reseal(run_copy)
    return Mutated(run_dir=run_copy)
