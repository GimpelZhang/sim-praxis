"""依赖准入质量审计实现（供 `scripts/s0/audit_physicalrsi.py` 调用；fail-closed）。

五项检查与豁免纪律见脚本 docstring。本模块是**单一实现**，脚本只做 CLI。
"""
from __future__ import annotations

import importlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

EXPECTED_VERSION = "0.3.0"
EXPECTED_CLASSES = {
    "EvolutionSpec": "PhysicalRSI_core.self_harness.artifacts",
    "ShadowGate": "PhysicalRSI_core.self_harness.shadow",
    "ResearchMemoryGate": "PhysicalRSI_core.self_harness.research_memory",
    "MemoryBoundSuite": "PhysicalRSI_core.self_harness.research_memory",
    "PreregisteredSuite": "PhysicalRSI_core.self_harness.protocol",
    "TrialQuota": "PhysicalRSI_core.infra.trial_quota",
    "ExperimentEvaluator": "PhysicalRSI_core.self_harness.evaluation",
    "ResearchMemory": "PhysicalRSI_core.self_harness.research_memory",
    "HarnessState": "PhysicalRSI_core.lineage",
    "CandidateArchive": "PhysicalRSI_core.self_harness.archive",
    "ResearchCampaign": "physicalrsi.research.workflow",
    "ParameterSpace": "PhysicalRSI_core.self_harness.search_space",
    "CallableSkill": "physicalrsi.skills.protocol",
    "SkillDescriptor": "physicalrsi.skills.protocol",
    "SkillRegistry": "physicalrsi",
    "Contract": "PhysicalRSI_core.contracts",
    "SkillMutation": "PhysicalRSI_core.self_harness.skill_mutation",
    "ModelConfig": "PhysicalRSI_core.infra.language",
}
TIMING_SENSITIVE_MODULE = "tests/test_infrastructure_faults.py"
FAIL_LINE = re.compile(r"^(FAILED|ERROR) (\S+)")


def _record(checks: list[dict], cid: str, name: str, ok: bool, detail: Any) -> bool:
    checks.append({"id": cid, "name": name, "pass": bool(ok), "detail": detail})
    return bool(ok)


def check_imports(checks: list[dict]) -> bool:
    from importlib.metadata import version as dist_version

    import physicalrsi

    installed = dist_version("physicalrsi")
    ok = _record(checks, "imports", "version + 18 classes importable",
                 installed == EXPECTED_VERSION,
                 {"installed_version": installed, "expected": EXPECTED_VERSION,
                  "physicalrsi_file": getattr(physicalrsi, "__file__", None)})
    missing: dict[str, str] = {}
    for name, module in EXPECTED_CLASSES.items():
        try:
            getattr(importlib.import_module(module), name)
        except Exception as exc:  # noqa: BLE001 - 逐条写进报告
            missing[name] = f"{module}: {type(exc).__name__}: {exc}"
    ok = _record(checks, "classes", "18 critical classes resolvable", not missing,
                 {"resolved": len(EXPECTED_CLASSES) - len(missing),
                  "total": len(EXPECTED_CLASSES), "missing": missing}) and ok
    return ok


def check_smoke(checks: list[dict]) -> bool:
    from PhysicalRSI_core.contracts import Contract, mismatches
    from physicalrsi.skills.compose import adapt, compose_sequence
    from physicalrsi.skills.protocol import (CallableSkill, CompositionTypeError,
                                             SkillDescriptor)

    manifest = Contract(name="run_manifest", unit="m", frame="world",
                        embodiment="mujoco_box_slide", shape=(None,), dtype="jsonl")
    result = Contract(name="analysis_result", unit="m", frame="world",
                      embodiment="mujoco_box_slide", shape=(None, 3), dtype="float64")
    spec = Contract(name="experiment_spec", dtype="json")

    def _skill(sid, cin, cout):
        return CallableSkill(SkillDescriptor(id=sid, revision="s0",
                                             input=cin, output=cout),
                             lambda value, ctx: value)

    k0 = _skill("k0_register_spec", spec, spec)
    k3 = _skill("k3_run_batch", spec, manifest)
    k5 = _skill("k5_analyze", manifest, result)
    detail: dict[str, Any] = {}
    ok = True
    try:
        compose_sequence("audit_smoke", k0, k3, k5).execute({"ok": True})
        detail["compose"] = "pass"
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail["compose"] = f"{type(exc).__name__}: {exc}"

    k5_bad = _skill("k5_bad_unit", Contract(name="run_manifest", unit="s"), result)
    try:
        compose_sequence("audit_bad", k3, k5_bad)
        ok = False
        detail["incompatible"] = "did NOT raise CompositionTypeError"
    except CompositionTypeError as exc:
        detail["incompatible"] = f"raised: {str(exc)[:160]}"

    detail["mismatches_unit"] = mismatches(manifest, Contract(name="run_manifest", unit="s"))
    detail["mismatches_shape"] = mismatches(manifest, Contract(name="run_manifest", shape=(None, 3)))
    if not detail["mismatches_unit"] or not detail["mismatches_shape"]:
        ok = False

    mm_contract = Contract(name="run_manifest", unit="mm", frame="world",
                           embodiment="mujoco_box_slide", shape=(None,), dtype="jsonl")
    adapter = adapt("m_to_mm", manifest, mm_contract, lambda value, ctx: value)
    k5_mm = _skill("k5_mm", mm_contract, result)
    try:
        compose_sequence("audit_adapt", k3, adapter, k5_mm)
        detail["adapt"] = "pass"
    except Exception as exc:  # noqa: BLE001
        ok = False
        detail["adapt"] = f"{type(exc).__name__}: {exc}"

    return _record(checks, "smoke", "Contract/skills/compose/adapt smoke", ok, detail)


def _pytest_run(repo: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
        cwd=repo, capture_output=True, text=True, timeout=1800)


def check_upstream_pytest(checks: list[dict], repo: Path) -> bool:
    proc = _pytest_run(repo, [])
    output = proc.stdout + proc.stderr
    failures = sorted({m.group(0) for m in
                       (FAIL_LINE.match(line) for line in output.splitlines()) if m})
    outside = [f for f in failures if TIMING_SENSITIVE_MODULE not in f]
    detail: dict[str, Any] = {
        "command": f"{sys.executable} -m pytest -q -p no:cacheprovider",
        "cwd": str(repo),
        "returncode": proc.returncode,
        "tail": output.strip().splitlines()[-6:],
        "failures": failures,
        "failures_outside_timing_module": outside,
        "timing_sensitive_module": TIMING_SENSITIVE_MODULE,
    }
    ok = not outside
    if failures and not outside:
        last_lines = []
        for _ in range(3):
            again = _pytest_run(repo, [TIMING_SENSITIVE_MODULE])
            last_lines.append(again.stdout.strip().splitlines()[-1] if again.stdout.strip() else "?")
        detail["timing_module_rerun_last_lines"] = last_lines
        counts = [int(m.group(1)) for line in last_lines if (m := re.search(r"(\d+) failed", line))]
        flaky = len(set(counts)) > 1
        detail["timing_module_flaky_demonstrated"] = flaky
        detail["timing_module_failed_counts"] = counts
        ok = ok and flaky
    elif proc.returncode != 0 and not failures:
        ok = False
    return _record(checks, "upstream_pytest", "clone pytest suite (verbatim result)", ok, detail)


def check_compileall(checks: list[dict]) -> bool:
    import PhysicalRSI_core
    import physicalrsi

    roots = [Path(physicalrsi.__file__).resolve().parent,
             Path(PhysicalRSI_core.__file__).resolve().parent]
    proc = subprocess.run([sys.executable, "-m", "compileall", "-q", *map(str, roots)],
                          capture_output=True, text=True, timeout=600)
    return _record(checks, "compileall", "compileall green", proc.returncode == 0,
                   {"roots": [str(r) for r in roots], "returncode": proc.returncode,
                    "stderr_tail": proc.stderr.strip().splitlines()[-4:]})


def check_license(checks: list[dict], repo: Path) -> bool:
    license_path = Path(repo) / "LICENSE"
    text = license_path.read_text(encoding="utf-8", errors="replace") \
        if license_path.exists() else ""
    ok = license_path.exists() and "Apache License" in text and "Version 2.0" in text
    return _record(checks, "license", "LICENSE Apache-2.0", ok,
                   {"path": str(license_path), "exists": license_path.exists(),
                    "head": text.strip().splitlines()[:2]})


def run_audit(repo: Path, *, skip_upstream_tests: bool = False) -> tuple[bool, list[dict]]:
    checks: list[dict] = []
    ok = check_imports(checks)
    ok = check_smoke(checks) and ok
    if skip_upstream_tests:
        _record(checks, "upstream_pytest", "clone pytest suite", False,
                {"skipped_for_negative_control": True})
        ok = False
    else:
        ok = check_upstream_pytest(checks, Path(repo)) and ok
    ok = check_compileall(checks) and ok
    ok = check_license(checks, Path(repo)) and ok
    return ok, checks
