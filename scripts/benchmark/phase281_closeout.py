#!/usr/bin/env python3
"""Phase 2.8.1 P281-9 收尾编排（0 付费；计划 §13.4–13.5）。

子命令（**可执行**顺序，与 Phase 2.8.0 ``closeout.py`` 同构）：

1. ``reports``     —— ``analysis/final_report.json`` → ``reports/final_report.md``
                      （必须在打包之前；报告是被扫的文本，不是判据本身）
2. ``cold-run``    —— 解析冷跑日志 → ``closeout/cold_run.json``（含 rc 侧车）
3. ``pack``        —— ``--clean`` 重建证据包 → ``pack.sha256``
4. ``matrix``      —— 写 ``closeout/environment.json`` + ``acceptance_matrix.json``
                      （判决件族，不入包；**验收车道之前**先跑一次，三签之后再
                      重跑一次让 AC-9-11 读到签）
5. ``acceptance``  —— 跑本阶段验收车道，落日志与 rc
6. （人工/分组）三签 —— ``closeout/signs/{executor,reviewer,verifier}.json``
                      绑 ``manifest.git_commit`` + ``pack.sha256``
7. ``matrix``（重跑）
8. ``judge``       —— ``check_phase281_closure.write_judge`` + 不动点两等式

纪律：0 付费、不 push、不改任何一本账；所有数字现读现算；判决件族不入包。
``closeout/`` 本身**不在** ``@bench`` 遍历范围（判决件都住里面），
这与 ``build_evidence_pack.BENCH_DIRS`` 的注释逐字同义。
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
for _extra in (str(_REPO), str(_REPO / "scripts" / "benchmark"),
               str(_REPO / "scripts" / "verification")):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

import build_evidence_pack as packer  # noqa: E402
import check_phase281_closure as judge  # noqa: E402
from orchestrator.benchmark.phase281 import analysis, constants, paths  # noqa: E402

MAIN_PYTHON = "/mnt/venvs/simpraxis/bin/python"
GPU_LIMIT_MIB = 200

#: ``@bench`` 遍历范围：数据根十四目去掉三处——``evidence_pack/``（包自身）、
#: ``closeout/``（判决件族住这里，§13.4「不入包」）、``dryrun/``（假网关产物）。
#: 其余十一目**全打**，包括 ``analysis/``：终局报告是证据，不是判据。
BENCH_DIRS_281 = ("intake", "pre_registration", "watchdog", "quota", "runs",
                  "candidate_cache", "references", "evaluator_results",
                  "analysis", "reports", "private")

#: ``@repo`` 范围：Phase 2.8.0 的五层 + 本阶段的机判脚本所在目录。
REPO_PATHS_281 = packer.REPO_PATHS + ("scripts/verification",)

ACCEPTANCE_TARGETS = ("tests/benchmark/test_phase281_analysis.py",
                      "tests/benchmark/test_phase281_closure_judge.py")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_json(path: Path, doc) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1,
                               sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


# --------------------------------------------------------------------------
# ① 报告
# --------------------------------------------------------------------------

def cmd_reports(root: Path) -> dict:
    src = Path(root) / "analysis" / "final_report.json"
    if not src.is_file():
        raise SystemExit(f"缺终局报告：{src}（先跑 analysis --stage final）")
    report = json.loads(src.read_text(encoding="utf-8"))
    text = analysis.render_report_markdown(report)
    out = Path(root) / "reports" / "final_report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    out.chmod(0o600)
    problems, detail = judge.check_report_text(root, text=text)
    return {"markdown": str(out), "sha256": _sha256(out),
            "disclaimer_sha256": detail.get("disclaimer_sha256"),
            "problems": problems}


# --------------------------------------------------------------------------
# ② 冷跑
# --------------------------------------------------------------------------

def cmd_cold_run(root: Path, log: Path, rc: "int | None") -> dict:
    """把冷跑日志解析成 ``closeout/cold_run.json``（rc 侧车优先，其次 ``--rc``）。"""
    log = Path(log)
    if not log.is_file():
        raise SystemExit(f"冷跑日志不存在：{log}")
    text = log.read_text(encoding="utf-8", errors="ignore")
    sidecar = log.with_suffix(log.suffix + ".rc")
    if sidecar.is_file():
        match = re.search(r"rc=(\d+)", sidecar.read_text(encoding="utf-8"))
        if match:
            rc = int(match.group(1))
    summary = judge_parse(text)
    failed = re.search(r"(\d+) failed", text)
    doc = {
        "schema": "simpraxis.phase281.cold_run.v1",
        "log": str(log),
        "log_sha256": _sha256(log),
        "rc": rc,
        "summary": summary,
        "failed": int(failed.group(1)) if failed else 0,
        "recorded_at": _now(),
    }
    out = _write_json(Path(root) / "closeout" / "cold_run.json", doc)
    return {"path": str(out), "rc": rc, "summary": summary}


def judge_parse(text: str) -> "dict | None":
    """复用 ``check_closure.parse_pytest_summary``（同一份摘要口径）。"""
    from check_closure import parse_pytest_summary

    return parse_pytest_summary(text)


# --------------------------------------------------------------------------
# ③ 打包
# --------------------------------------------------------------------------

def cmd_pack(root: Path, clean: bool) -> dict:
    result = packer.build(root=root, pack=Path(root) / "evidence_pack",
                          clean=clean, bench_dirs=BENCH_DIRS_281,
                          repo_paths=REPO_PATHS_281)
    return {"pack_sha256": result["pack_sha256"], "n_files": result["n_files"],
            "total_bytes": result["total_bytes"]}


# --------------------------------------------------------------------------
# ④ 环境探测（AC-9-17 的数据源）
# --------------------------------------------------------------------------

def environment_doc() -> dict:
    """容器 / 显存 / ``/mnt`` 三项现场探测。

    复用分析的 ``env_probe()``（它已带「compose 件在 sandbox/」的锁内适配），
    另加 ``/mnt`` 空间——**rc≠0 时「0 个容器」是没看见，不是干净**，这一条由
    机判强制（``check_environment``）。
    """
    env = analysis.env_probe()
    df_mnt = ""
    try:
        proc = subprocess.run(["df", "-h", "/mnt"], capture_output=True,
                              text=True, timeout=60, check=False)
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        df_mnt = lines[-1] if lines else ""
    except (OSError, subprocess.SubprocessError):
        df_mnt = ""
    return {
        "schema": "simpraxis.phase281.environment.v1",
        "containers": env.get("containers"),
        "compose_rc": env.get("rc"),
        "gpu_mib": env.get("gpu_mib"),
        "df_mnt": df_mnt,
        "probe_error": env.get("error"),
        "recorded_at": _now(),
    }


# --------------------------------------------------------------------------
# ⑤ 验收矩阵
# --------------------------------------------------------------------------

def _fixed_point_prereq(root: Path) -> "tuple[list[str], dict]":
    """AC-9-10 在**判决件写盘之前**可判的那一半：确定性 + 族命中。

    等式①（判决件 ``pack_sha256`` == ``pack.sha256`` 实际内容）在判决件存在
    之前无从谈起，由 ``judge`` 写完后的 ``verify_fixed_point`` 强制；
    矩阵不假装自己验过它。
    """
    problems: "list[str]" = []
    if not judge._is_judgment_relpath(judge.JUDGE_RELPATH):  # noqa: SLF001
        problems.append("判决件路径未被判决件族规则命中")
    first = judge.judge_text(judge.run_judge(root))
    second = judge.judge_text(judge.run_judge(root))
    if first != second:
        problems.append("同输入两次 run_judge 输出不逐字节相同")
    return problems, {"deterministic": first == second,
                      "red_checks": [name for name, row in
                                     json.loads(first)["checks"].items()
                                     if row["problems"]]}


def matrix_rows(root: Path) -> dict:
    """逐 AC 的机判行（**判据即判据**：与终判调同一批 ``check_*``）。"""
    root = Path(root)
    results = {}
    for name in judge.CHECKS:
        if name in ("ac_index", "gaps"):
            continue
        fn = judge._CHECKS.get(name)  # noqa: SLF001
        if fn is None:
            continue
        try:
            problems, detail = fn(root)
        except Exception as exc:  # noqa: BLE001
            problems, detail = [f"检查抛异常：{type(exc).__name__}: {exc}"], {}
        results[name] = {"problems": list(problems), "detail": detail}
    fp_problems, fp_detail = _fixed_point_prereq(root)
    results["fixed_point"] = {"problems": fp_problems, "detail": fp_detail}
    gap_problems, gaps = judge.collect_gaps(root)
    results["gaps"] = {"problems": gap_problems, "detail": {"gaps": gaps}}

    rows = []
    for ac in judge.AC_IDS:
        names = list(judge.AC_COVERAGE[ac])
        names = ["fixed_point" if n == "verify_fixed_point" else n
                 for n in names]
        names = [n for n in names if n != "ac_index"]
        problems: "list[str]" = []
        for name in names:
            problems.extend(results.get(name, {"problems": [f"无此检查 {name}"]}
                                       )["problems"])
        rows.append({"ac": ac, "checks": names, "ok": not problems,
                     "problems": problems})
    # AC-9-12 只能由「其余行全绿 + gaps 合法」推出（它自己不参与计算）
    others = [r for r in rows if r["ac"] != "AC-P281-9-12"]
    judge_ok = all(r["ok"] for r in others) and not results["gaps"]["problems"]
    for row in rows:
        if row["ac"] == "AC-P281-9-12":
            row["ok"] = judge_ok
            if not judge_ok:
                row["problems"] = (["其余 AC 未全绿"] if not all(
                    r["ok"] for r in others) else []) + \
                    results["gaps"]["problems"]
    return {"rows": rows, "gaps": gaps,
            "red": [r["ac"] for r in rows if not r["ok"]]}


def cmd_matrix(root: Path) -> dict:
    root = Path(root)
    _write_json(root / "closeout" / "environment.json", environment_doc())
    doc = matrix_rows(root)
    passed = sum(1 for r in doc["rows"] if r["ok"])
    manifest = root / "evidence_pack" / "pack.sha256"
    matrix = {
        "schema": "simpraxis.phase281.acceptance_matrix.v1",
        "pack_sha256": (manifest.read_text(encoding="utf-8").strip()
                        if manifest.is_file() else None),
        "rows": doc["rows"],
        "gaps": doc["gaps"],
        "summary": {"total": len(doc["rows"]), "passed": passed,
                    "failed": len(doc["rows"]) - passed},
        "recorded_at": _now(),
    }
    out = _write_json(root / "closeout" / "acceptance_matrix.json", matrix)
    return {"path": str(out), "summary": matrix["summary"],
            "red": doc["red"]}


# --------------------------------------------------------------------------
# ⑥ 验收车道
# --------------------------------------------------------------------------

def cmd_acceptance(root: Path) -> dict:
    out_dir = Path(root) / "closeout"
    out_dir.mkdir(parents=True, exist_ok=True)
    log = out_dir / "acceptance_lane.log"
    cmd = [MAIN_PYTHON, "-m", "pytest", *ACCEPTANCE_TARGETS, "-q"]
    env = dict(os.environ, SIMPRAXIS_PHASE281_ROOT=str(Path(root).resolve()))
    proc = subprocess.run(cmd, cwd=str(_REPO), env=env, capture_output=True,
                          text=True, timeout=3600)
    text = f"$ {' '.join(cmd)}\n(rc={proc.returncode})\n{proc.stdout}{proc.stderr}"
    log.write_text(text, encoding="utf-8")
    log.chmod(0o600)
    summary = judge_parse(text) or {"passed": 0, "skipped": 0,
                                    "deselected": 0, "duration_s": 0.0}
    failed = re.search(r"(\d+) failed", text)
    doc = {
        "schema": "simpraxis.phase281.acceptance_lane.v1",
        "cmd": " ".join(cmd),
        "rc": proc.returncode,
        "passed": summary["passed"],
        "failed": int(failed.group(1)) if failed else 0,
        "skipped": summary["skipped"],
        "deselected": summary["deselected"],
        "log": str(log),
        "log_sha256": _sha256(log),
        "recorded_at": _now(),
    }
    _write_json(out_dir / "acceptance_lane.json", doc)
    return {k: doc[k] for k in ("rc", "passed", "failed", "skipped")}


# --------------------------------------------------------------------------
# ⑦ 终判
# --------------------------------------------------------------------------

def cmd_judge(root: Path, require_acceptance: bool) -> dict:
    root = Path(root)
    lane = root / "closeout" / "acceptance_lane.json"
    if require_acceptance and not lane.is_file():
        raise SystemExit(f"缺验收车道记录：{lane}（先跑 acceptance）")
    path = judge.write_judge(root)
    doc = json.loads(path.read_text(encoding="utf-8"))
    fp_problems, fp_detail = judge.verify_fixed_point(root)
    return {"path": str(path), "verdict": doc["verdict"],
            "problems": doc["problems"], "gaps": doc["gaps"],
            "fixed_point": {"problems": fp_problems, **fp_detail}}


# --------------------------------------------------------------------------

def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=None, help="数据根（缺省 paths.phase281_root()）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("reports")
    cold = sub.add_parser("cold-run")
    cold.add_argument("--log", required=True)
    cold.add_argument("--rc", type=int, default=None)
    pack = sub.add_parser("pack")
    pack.add_argument("--clean", action="store_true")
    sub.add_parser("matrix")
    sub.add_parser("acceptance")
    judgment = sub.add_parser("judge")
    judgment.add_argument("--require-acceptance", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.root) if args.root else paths.phase281_root(read_only=True)
    if args.cmd == "reports":
        out = cmd_reports(root)
    elif args.cmd == "cold-run":
        out = cmd_cold_run(root, Path(args.log), args.rc)
    elif args.cmd == "pack":
        out = cmd_pack(root, args.clean)
    elif args.cmd == "matrix":
        out = cmd_matrix(root)
    elif args.cmd == "acceptance":
        out = cmd_acceptance(root)
    else:
        out = cmd_judge(root, args.require_acceptance)
    print(json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True,
                     default=str))
    if args.cmd == "judge":
        return 0 if out["verdict"] == "PASS" and \
            not out["fixed_point"]["problems"] else 1
    if args.cmd == "matrix":
        return 0 if int(out["summary"]["failed"]) == 0 else 1
    if args.cmd == "acceptance":
        return 0 if out["rc"] == 0 and out["failed"] == 0 and out["passed"] > 0 else 1
    if args.cmd == "reports":
        return 0 if not out["problems"] else 1
    if args.cmd == "cold-run":
        return 0 if out["rc"] == 0 else 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
