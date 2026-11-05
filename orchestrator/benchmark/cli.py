"""``python -m orchestrator.benchmark.cli <subcommand>`` 统一入口。

首版只挂 ``preflight``（Task 0）。后续 Task 逐步加子命令，一律保持
「薄 CLI + 纯函数库」的形状：CLI 只负责解析参数、调库、落盘、打印摘要。

``preflight`` 的职责（T0.5 第 3/4 步）：
1. 依次调 ``intake`` 的七个采集函数；
2. 把七个 JSON 原子写进 ``<bench_root>/intake/``（0600）；
3. 渲染 ``intake_report.md``（表格里的每个值都来自 JSON，AC-T0-8 机判交叉核对）；
4. 顺带跑 ``gate_static.sh`` 与 ``--collect-only``，原始输出落 ``cmd_logs/``；
5. 任何 STOP 条件（仓脏 / 门禁红 / 冷跑红 / KB 脏）先落盘再以退出码 3 报告。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from orchestrator.benchmark import intake, paths
from orchestrator.common.fsutil import atomic_write_text

EXIT_OK = 0
EXIT_STOP = 3

_EMPTY = "(空)"


def _write_json(path: Path, doc: dict) -> Path:
    return atomic_write_text(
        path, json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        mode=0o600)


def _cell(value) -> str:
    if value is None:
        return "(null)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        if "\n" in value:
            # 多行值会把 Markdown 表格撑破 → 逐行交叉核对静默漏检（AC-T0-8 的洞）
            raise ValueError(f"报告单元格不得含换行：{value[:60]!r}")
        return value if value else _EMPTY
    return str(value)


def _rows(section: str, doc_name: str, items: "list[tuple[str, object]]") -> list:
    out = [f"### {section}", "", "| 项 | 值 | 来源 JSON |", "|---|---|---|"]
    for key, value in items:
        out.append(f"| {key} | {_cell(value)} | {doc_name} |")
    out.append("")
    return out


def render_report(docs: "dict[str, dict]") -> str:
    """把七个 JSON 渲染成 Markdown；**每个值都是 JSON 里的原值**。"""
    repo = docs["repository_state"]
    kb = docs["kb_state"]
    tb = docs["test_baseline"]
    env = docs["environment_baseline"]
    ledgers = docs["sealed_ledger_sha256"]["ledgers"]
    tables = docs["table_counts"]["tables"]
    inv = docs["invariants_before"]

    lines = [
        "# Phase 2.8.0 Task 0 基线采集报告",
        "",
        f"- 生成器：`{intake.GENERATED_BY}`",
        f"- 生成时间：{repo['ts_iso']}",
        f"- 引擎仓 HEAD：`{repo['head']}`",
        f"- 数据根：`{inv['bench_root']}`",
        "",
        "> 本报告的所有数值均由 `intake/*.json` 渲染，禁止手改；"
        "机判 `test_ac_p0_0_8_report_crosscheck` 逐行回查 JSON。",
        "",
    ]
    lines += _rows("1. 引擎仓状态", "repository_state.json", [
        ("head", repo["head"]),
        ("origin_main", repo["origin_main"]),
        ("ahead", repo["ahead"]),
        ("status_porcelain", repo["status_porcelain"]),
        ("diff_stat", repo["diff_stat"]),
        ("status_porcelain_excluded_count",
         len(repo["status_porcelain_excluded"])),
    ])
    lines += _rows("2. KB 仓状态", "kb_state.json", [
        ("head", kb["head"]),
        ("origin_main", kb["origin_main"]),
        ("status_porcelain", kb["status_porcelain"]),
        ("diff_stat", kb["diff_stat"]),
    ])
    lines += _rows("3. 测试基线（raw log 解析）", "test_baseline.json", [
        ("passed", tb["passed"]),
        ("skipped", tb["skipped"]),
        ("deselected", tb["deselected"]),
        ("duration_s", tb["duration_s"]),
        ("rc", tb["rc"]),
        ("raw_log_sha256", tb["raw_log_sha256"]),
        ("collect_only", tb.get("collect_only", "")),
    ])
    lines += _rows("4. 环境锚点", "environment_baseline.json", [
        ("python", env["python"]),
        ("docker_version", env["docker_version"]),
        ("gpu_name", env["gpu_name"]),
        ("gpu_total_mib", env["gpu_total_mib"]),
        ("gpu_used_mib", env["gpu_used_mib"]),
        ("df_mnt_available_gib", env["df_mnt_available_gib"]),
        ("mujoco", env["packages"].get("mujoco")),
    ])
    ledger_rows = []
    for phase in intake.LEDGER_PHASES:
        ledger_rows.append((f"{phase}.rows", ledgers[phase]["rows"]))
        ledger_rows.append((f"{phase}.sha256", ledgers[phase]["sha256"]))
    lines += _rows("5. 五本账（只读冻结）", "sealed_ledger_sha256.json",
                   ledger_rows)
    lines += _rows("6. 四张活表（只读行数）", "table_counts.json",
                   [(name, tables[name]) for name in intake.TABLES]
                   + [("drift_note", docs["table_counts"]["drift_note"])])
    lines += _rows("7. 不变量 before 基线", "invariants_before.json", [
        ("stage_models_sha256", inv["stage_models_sha256"]),
        ("phase27_pack_sha256", inv["phase27_pack_sha256"]),
        ("phase27_manifest_sha256", inv["phase27_manifest_sha256"]),
        ("phase27_pack_n_files", inv["phase27_pack_n_files"]),
        ("phase27_pack_total_bytes", inv["phase27_pack_total_bytes"]),
        ("entrypoint_ready_source_sha256",
         inv["entrypoint_ready_source_sha256"]),
    ])
    lines += [
        "## 命令留痕",
        "",
        "`intake/cmd_logs/` 逐条保存原始 stdout/stderr（追加写，重跑留痕）：",
        "",
        "| 日志 | 内容 |",
        "|---|---|",
        "| `01_repository_state.log` | 引擎仓 git 状态 |",
        "| `02_kb_state.log` | KB 仓 git 状态 |",
        "| `03_sealed_ledgers.log` | 五本账行数/sha256 |",
        "| `05_gate_static.log` | 六步静态门禁 |",
        "| `06_pytest_two_exclusion.log` | 两排除全套冷跑 raw log |",
        "| `07_collect_only.log` | 收集口径 |",
        "| `08_environment_baseline.log` | python/docker/gpu/df |",
        "| `09_invariants_before.log` | sha256sum stage_models.yaml |",
        "",
    ]
    return "\n".join(lines)


def _stop_conditions(docs: "dict[str, dict]", gate_rc: int) -> "list[str]":
    problems = []
    repo, kb, tb = docs["repository_state"], docs["kb_state"], docs["test_baseline"]
    if repo["status_porcelain"]:
        problems.append(
            "STOP-A 引擎仓工作树非空（已剔除本阶段新增路径后仍有改动）："
            f"{repo['status_porcelain']!r}")
    if kb["head"] != "5f0bb17e91c9215536863b40f191a07b8f47ef35" or \
            kb["status_porcelain"]:
        problems.append(
            f"STOP-B KB 漂移：head={kb['head']} dirty={bool(kb['status_porcelain'])}")
    if gate_rc != 0:
        problems.append(f"STOP-C 静态门禁 rc={gate_rc}")
    if tb["rc"] != 0:
        problems.append(f"STOP-C 两排除全套 rc={tb['rc']}")
    return problems


def cmd_preflight(args: argparse.Namespace) -> int:
    root = paths.ensure_layout()
    raw_log = Path(args.raw_log) if args.raw_log else (
        paths.cmd_logs_dir() / "06_pytest_two_exclusion.log")

    if args.run_tests:
        cmd = intake.TWO_EXCLUSION_CMD.format(py=sys.executable)
        argv = ["bash", "-lc", f"{cmd}; echo rc=$?"]
        with open(raw_log, "w", encoding="utf-8") as fh:
            fh.write("")
        proc = subprocess.run(argv, cwd=str(intake.REPO_ROOT),
                              capture_output=True, text=True)
        raw_log.write_text(proc.stdout + proc.stderr, encoding="utf-8")
        raw_log.chmod(0o600)

    docs = {
        "repository_state": intake.repository_state(),
        "kb_state": intake.kb_state(),
        "test_baseline": intake.test_baseline(raw_log),
        "environment_baseline": intake.environment_baseline(),
        "sealed_ledger_sha256": intake.sealed_ledger_sha256(),
        "table_counts": intake.table_counts(),
        "invariants_before": intake.invariants_before(),
    }

    # 附加证据：静态门禁 + 收集口径（都落 cmd_logs）
    gate = intake.run_command(
        ["bash", "scripts/verification/gate_static.sh"],
        cwd=intake.REPO_ROOT, timeout=600,
        log=paths.cmd_logs_dir() / "05_gate_static.log")
    collect = intake.run_command(
        [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q"],
        cwd=intake.REPO_ROOT, timeout=600,
        log=paths.cmd_logs_dir() / "07_collect_only.log")
    collect_line = ""
    for line in reversed(collect["stdout_tail"].splitlines()):
        if "collected" in line or "deselected" in line:
            collect_line = line.strip()
            break
    docs["test_baseline"]["collect_only"] = collect_line

    for name, doc in docs.items():
        _write_json(paths.intake_dir() / f"{name}.json", doc)
    report = render_report(docs)
    atomic_write_text(paths.intake_dir() / "intake_report.md", report, 0o600)

    problems = _stop_conditions(docs, gate["rc"])
    print(f"[preflight] bench_root={root}")
    print(f"[preflight] HEAD={docs['repository_state']['head']} "
          f"ahead={docs['repository_state']['ahead']} "
          f"baseline={docs['test_baseline']['passed']} passed / "
          f"{docs['test_baseline']['skipped']} skipped / "
          f"{docs['test_baseline']['deselected']} deselected "
          f"in {docs['test_baseline']['duration_s']}s")
    print(f"[preflight] gate_static rc={gate['rc']}  "
          f"report={paths.intake_dir() / 'intake_report.md'}")
    for problem in problems:
        print(f"[preflight] {problem}")
    if problems:
        print("[preflight] 结论：STOP（先报用户，不自动改期望值）")
        return EXIT_STOP
    print("[preflight] 结论：OK")
    return EXIT_OK


# --------------------------------------------------------------------------
# Task 1：validate-specs / lock-criteria
# --------------------------------------------------------------------------

_PHASE = "roadmap_phase0"


def _specs_report(reg) -> dict:
    """任务定义的全量结构化校验结果（Task 1 证据件 validation_report.json）。"""
    from orchestrator.benchmark import registry, task_spec as ts

    report = {
        "schema": "simpraxis.benchmark_validation_report.v1",
        "generated_by": intake.GENERATED_BY,
        "ts_iso": intake._now(),
        "benchmark_root": str(reg.root),
        "tasks": [],
        "manifest_check": {},
        "problems": [],
        "ok": False,
    }
    try:
        specs = reg.load_all(verify_manifest=False)
    except (ts.TaskSpecError, registry.RegistryError) as exc:
        report["problems"].append(f"{type(exc).__name__}: {exc}")
        return report

    manifest = reg.check_manifest()
    report["manifest_check"] = manifest
    report["problems"].extend(manifest["problems"])
    report["manifest_sha256"] = reg.manifest_sha256()
    report["criteria_lock"] = reg.criteria_aggregate_sha256()
    for tid in sorted(specs):
        spec = specs[tid]
        criteria = spec.criteria
        report["tasks"].append({
            "task_id": tid,
            "version": spec.version,
            "track": spec.track,
            "task_type": spec.task_type,
            "criteria_version": criteria["criteria_version"],
            "criteria_lock_sha256": spec.criteria_lock_sha256,
            "criteria_sha256": spec.criteria_sha256,
            "criteria_file": spec.criteria_file,
            "task_file": spec.task_file,
            "task_file_sha256": spec.task_file_sha256,
            "weights": dict(criteria["weights"]),
            "weights_sum": round(sum(float(w) for w in criteria["weights"].values()), 12),
            "thresholds": {
                key: {"value": item["value"], "unit": item["unit"],
                      "derivation_chars": len(item["derivation"])}
                for key, item in criteria["thresholds"].items()
            },
            "hard_gates": [gate["id"] for gate in criteria["hard_gates"]],
            "outputs": list(spec.outputs),
            "model_calls": spec.budget["model_calls"],
            "birth_contract": dict(spec.birth_contract),
            "seeds": list(spec.reproducibility["seeds"]),
        })
    if len(report["tasks"]) != len(specs):
        report["problems"].append("任务快照数与加载数不符")
    report["ok"] = not report["problems"]
    return report


def cmd_validate_specs(args: argparse.Namespace) -> int:
    from orchestrator.benchmark import registry, task_spec as ts

    root = paths.specs_dir()
    reg = registry.BenchmarkRegistry()
    report = _specs_report(reg)
    _write_json(root / "validation_report.json", report)
    _write_json(root / "registry_snapshot.json", reg.snapshot())

    schemas = {}
    for path in sorted((reg.root / "schemas").glob("*.schema.json")):
        schemas[path.name] = ts.sha256_file(path)
    _write_json(root / "schemas_sha256.json", {
        "schema": "simpraxis.benchmark_schemas_sha256.v1",
        "generated_by": intake.GENERATED_BY,
        "ts_iso": intake._now(),
        "schemas_dir": str(reg.root / "schemas"),
        "schemas": schemas,
    })

    print(f"[validate-specs] root={reg.root}")
    print(f"[validate-specs] tasks={[t['task_id'] for t in report['tasks']]}")
    print(f"[validate-specs] manifest_sha256={report.get('manifest_sha256')}")
    print(f"[validate-specs] criteria_lock={report.get('criteria_lock')}")
    for problem in report["problems"]:
        print(f"[validate-specs] PROBLEM {problem}")
    print(f"[validate-specs] 结论：{'OK' if report['ok'] else 'STOP'}")
    return EXIT_OK if report["ok"] else EXIT_STOP


def _replace_scalar(path: Path, key: str, value: str) -> "tuple[str, str]":
    """逐行替换 ``key: "..."``（保住 YAML 注释；恰好一处，否则报错）。"""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    hits = [i for i, line in enumerate(lines)
            if line.strip().startswith(f"{key}:")]
    if len(hits) != 1:
        raise RuntimeError(f"{path}: 期望恰好 1 行 `{key}:`，实为 {len(hits)}")
    index = hits[0]
    old = lines[index].split(":", 1)[1].strip().strip('"')
    indent = lines[index][:len(lines[index]) - len(lines[index].lstrip())]
    lines[index] = f'{indent}{key}: "{value}"'
    atomic_write_text(path, "\n".join(lines) + "\n", 0o644)
    return old, value


def cmd_lock_criteria(args: argparse.Namespace) -> int:
    """按 D1-5 规范子集重算并回填 criteria_lock_sha256（Task 1 冻结用）。"""
    import yaml

    from orchestrator.benchmark import registry, task_spec as ts

    reg = registry.BenchmarkRegistry()
    locks: "dict[str, str]" = {}
    rows = []
    crit_files = sorted((reg.root / "criteria").glob("*.criteria.json"))
    if not crit_files:
        print(f"[lock-criteria] 判据目录为空：{reg.root / 'criteria'}")
        return EXIT_STOP
    task_yamls = sorted(reg.tasks_dir.glob("*/task.yaml"))
    for crit_path in crit_files:
        doc = json.loads(crit_path.read_text(encoding="utf-8"))
        ts.validate_criteria(doc, crit_path)
        lock = ts.criteria_lock_sha256(doc)
        task_id = doc.get("task_id")
        if task_id != crit_path.name.replace(".criteria.json", ""):
            print(f"[lock-criteria] {crit_path.name}: task_id={task_id!r} 与文件名不符")
            return EXIT_STOP
        locks[task_id] = lock
        matched = []
        for task_yaml in task_yamls:
            task_doc = yaml.safe_load(task_yaml.read_text(encoding="utf-8"))
            if task_doc.get("evaluation", {}).get("evaluator_id") == task_id:
                matched.append(task_yaml)
        if len(matched) != 1:
            print(f"[lock-criteria] {task_id} 匹配到 {len(matched)} 个 task.yaml")
            return EXIT_STOP
        _replace_scalar(matched[0], "criteria_lock_sha256", lock)
        rows.append((task_id, str(matched[0].relative_to(reg.root)), lock))

    aggregate = ts.criteria_aggregate_sha256(locks)
    _replace_scalar(reg.manifest_path, "criteria_lock", aggregate)
    for task_id, task_file, lock in rows:
        print(f"[lock-criteria] {task_id:<26} {lock}  <- {task_file}")
    print(f"[lock-criteria] manifest.criteria_lock={aggregate}")
    print("[lock-criteria] 结论：OK（现在跑 validate-specs 复核）")
    return EXIT_OK


def cmd_capture_runtime(args: argparse.Namespace) -> int:
    """Task 2：在真正执行任务的环境里采集并冻结六段指纹（0 付费）。"""
    from orchestrator.benchmark import registry, runtime_fingerprint as rf

    reg = registry.BenchmarkRegistry()
    try:
        spec = reg.get(args.task)
    except Exception as exc:               # noqa: BLE001 - 未登记任务即停
        print(f"[capture-runtime] 取任务失败：{exc}")
        return EXIT_STOP

    image = args.image or spec.runtime.get("image_digest")
    backend = args.backend
    if backend == "auto":
        backend = "docker" if image else "host"
    if backend == "docker" and not args.image_name:
        args.image_name = {"libero_env_adapter_v1": "sim-praxis/libero:task6"}.get(
            spec.task_id)
    try:
        doc, capture = rf.capture_and_build(
            spec, image=image, backend=backend, image_name=args.image_name)
        out_dir = Path(args.dir) if args.dir else (paths.fingerprints_dir() / spec.task_id)
        written = rf.write_fingerprint(out_dir, doc, capture)
    except rf.FingerprintError as exc:
        print(f"[capture-runtime] {spec.task_id}: {exc}")
        return EXIT_STOP

    print(f"[capture-runtime] task={spec.task_id} backend={capture.backend}")
    print(f"[capture-runtime] python={doc['python']['executable']} "
          f"version={doc['python']['version']}")
    sim = doc.get("simulator") or {}
    print(f"[capture-runtime] simulator={sim.get('name')}@{sim.get('version')} "
          f"image={doc['container']['image_digest']}")
    print(f"[capture-runtime] fingerprint_sha256={written['fingerprint_sha256']}")
    print(f"[capture-runtime] wrote {written['fingerprint_path']}")
    return EXIT_OK


def cmd_birth(args: argparse.Namespace) -> int:
    """Task 3：对候选 workdir 跑 B0→B4 出生验证并落盘（0 付费）。"""
    import datetime as _dt

    from orchestrator.benchmark import birth, registry

    reg = registry.BenchmarkRegistry()
    try:
        spec = reg.get(args.task)
    except Exception as exc:               # noqa: BLE001 - 未登记任务即停
        print(f"[birth] 取任务失败：{exc}")
        return EXIT_STOP

    workdir = Path(args.workdir)
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.out) if args.out else (
        paths.runs_dir() / f"{spec.task_id}-birth-{stamp}" / "birth")
    try:
        bspec = birth.BirthSpec.from_task_spec(spec)
        validator = birth.BirthValidator(
            bspec, workdir=workdir,
            limits=birth.Limits(timeout_s=args.timeout),
            network_mode=args.network_mode)
        result = validator.validate(out_dir)
        written = birth.write_birth(out_dir, result)
    except birth.BirthError as exc:
        print(f"[birth] {spec.task_id}: {exc}")
        return EXIT_STOP

    print(f"[birth] task={spec.task_id} workdir={validator.workdir}")
    print(f"[birth] contract={bspec.contract} required={bspec.required_levels()}")
    for level in birth.LEVELS:
        item = result.levels[level]
        print(f"[birth] {level} {item.status:<14} reason={item.reason} "
              f"{item.detail[:120]}")
    for problem in result.problems:
        print(f"[birth] PROBLEM {problem}")
    print(f"[birth] birth_status={result.birth_status} "
          f"task_success=None sha256={written['birth_sha256']}")
    print(f"[birth] wrote {written['birth_path']}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator.benchmark.cli",
        description="Phase 2.8.0 Benchmark 基座 CLI（0 付费）")
    sub = parser.add_subparsers(dest="command", required=True)

    pf = sub.add_parser("preflight", help="Task 0：七类基线采集与冻结")
    pf.add_argument("--phase", default=_PHASE, choices=[_PHASE])
    pf.add_argument("--raw-log", default=None,
                    help="两排除全套的 raw log（缺省 cmd_logs/06_...log）")
    pf.add_argument("--run-tests", action="store_true",
                    help="先真跑一次两排除全套再解析（≈470 s）")
    pf.set_defaults(func=cmd_preflight)

    vs = sub.add_parser("validate-specs", help="Task 1：任务定义与判据锁全量校验")
    vs.add_argument("--phase", default=_PHASE, choices=[_PHASE])
    vs.set_defaults(func=cmd_validate_specs)

    lc = sub.add_parser("lock-criteria",
                        help="Task 1：按 D1-5 规范子集回填判据锁（冻结用）")
    lc.add_argument("--phase", default=_PHASE, choices=[_PHASE])
    lc.set_defaults(func=cmd_lock_criteria)

    cr = sub.add_parser("capture-runtime",
                        help="Task 2：采集并冻结任务运行环境的六段指纹")
    cr.add_argument("--task", required=True, help="task_id（如 mujoco_contact_push_v1）")
    cr.add_argument("--backend", default="auto", choices=["auto", "host", "docker"])
    cr.add_argument("--image", default=None,
                    help="image@sha256:... 覆盖 task.yaml.runtime.image_digest")
    cr.add_argument("--image-name", default=None, help="镜像名（仅 docker 后端）")
    cr.add_argument("--dir", default=None,
                    help="输出目录（缺省 <bench_root>/fingerprints/<task_id>）")
    cr.set_defaults(func=cmd_capture_runtime)

    bt = sub.add_parser("birth", help="Task 3：B0→B4 出生验证（Birth ≠ Task Success）")
    bt.add_argument("--task", required=True, help="task_id（如 libero_env_adapter_v1）")
    bt.add_argument("--workdir", required=True, help="候选交付物目录（绝对路径）")
    bt.add_argument("--out", default=None,
                    help="输出目录（缺省 <bench_root>/runs/<task>-birth-<stamp>/birth）")
    bt.add_argument("--timeout", type=float, default=120.0,
                    help="每层子进程墙钟上限（秒）")
    bt.add_argument("--network-mode", default="audited_only",
                    choices=["none", "audited_only"],
                    help="none=容器内 --network none（enforced）；默认宿主 audited_only")
    bt.set_defaults(func=cmd_birth)
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
