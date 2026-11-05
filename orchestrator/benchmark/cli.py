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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator.benchmark.cli",
        description="Phase 2.8.0 Benchmark 基座 CLI（0 付费）")
    sub = parser.add_subparsers(dest="command", required=True)

    pf = sub.add_parser("preflight", help="Task 0：七类基线采集与冻结")
    pf.add_argument("--raw-log", default=None,
                    help="两排除全套的 raw log（缺省 cmd_logs/06_...log）")
    pf.add_argument("--run-tests", action="store_true",
                    help="先真跑一次两排除全套再解析（≈470 s）")
    pf.set_defaults(func=cmd_preflight)
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
