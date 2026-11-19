#!/usr/bin/env python3
"""RSI-S1 独立复跑（S1-7 前半；冷进程 + 新证据根）。

口径（计划 S1-7）：

* **新进程**（本脚本本身）+ **新证据根**（``<evidence>/independent_rerun/<study>``）；
* 直接读 **K6 冻结计划**（conditions/blocks/runs/contrasts，frozen=true）；
* 走 ``host_recompute`` 在干净进程里按 (condition, block) 重仿真，逐点 (t,qpos,qvel)
  与原始 raw 比对（tol=0.0 位级）；不一致即停止码，不重跑求解；
* 重算逐对比统计，与 K7 的 host 结果**逐字段**比较（estimate/ci_low/ci_high/n_blocks）。

产物：``independent_rerun.json``（逐 study：n_diffs、contrast 比对、frozen 源 sha）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from orchestrator.common.clock import utc_now_iso_z  # noqa: E402
from orchestrator.common.fsutil import atomic_write_text, ensure_private_dir  # noqa: E402
from orchestrator.rsi_bridge import host_recompute as HR  # noqa: E402

SCHEMA = "simpraxis.rsi_s1.independent_rerun.v1"


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _contrast_map(rows) -> dict[str, dict[str, Any]]:
    return {str(r.get("id")): r for r in rows or [] if isinstance(r, Mapping)}


def rerun_study(study_dir: Path, study: str, out_root: Path,
                stamp: str = "") -> dict[str, Any]:
    k6 = _load(study_dir / "K6.json")
    k7 = _load(study_dir / "K7.json")
    k4 = _load(study_dir / "K4.json")
    if k6.get("frozen") is not True:
        raise SystemExit(f"NOT_FROZEN: {study} K6")
    # 每次复跑独立根（RAW_EXISTS 是纪律：不覆盖、不删除旧证据）
    fresh = ensure_private_dir(out_root / f"{study}-{stamp}", mode=0o700)
    handle = HR.recompute(k6, fresh_root=fresh, batch_id=f"rerun-{study}")
    diffs = [d.to_dict() for d in handle.diffs]
    recomputed = _contrast_map(handle.confirm_result.get("contrasts"))
    original = _contrast_map(k7.get("contrasts"))
    k8 = _load(study_dir / "K8.json")
    mismatches = []
    for cid in sorted(set(original) - set(recomputed)):
        mismatches.append(f"{cid}: missing_in_rerun")
    expected_verdict = None
    if handle.confirm_result.get("contrasts"):
        expected_verdict = handle.confirm_result["contrasts"][0].get("claim")
    if expected_verdict is not None and k8.get("verdict") != expected_verdict:
        mismatches.append(f"verdict: K8={k8.get('verdict')!r} != rerun={expected_verdict!r}")
    for cid, row in recomputed.items():
        other = original.get(cid)
        if other is None:
            mismatches.append(f"{cid}: missing_in_K7")
            continue
        for key in ("estimate", "ci_low", "ci_high", "n_blocks"):
            if row.get(key) != other.get(key):
                mismatches.append(f"{cid}.{key}: rerun={row.get(key)} != K7={other.get(key)}")
    return {
        "study": study,
        "n_runs": len(k6.get("runs") or []),
        "n_diffs": len(diffs),
        "diffs_head": diffs[:5],
        "contrasts_match_K7": not mismatches,
        "contrast_mismatches": mismatches,
        "recomputed": recomputed,
        "frozen_analysis_sha256": k6.get("analysis_sha256"),
        "analysis_program_sha256": k4.get("sha256"),
        "fresh_root": str(fresh),
        "confirmed_at_utc": handle.generated_at_utc,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RSI-S1 独立复跑")
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--studies", nargs="+", default=["A_N", "B_N"])
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    evidence = Path(args.evidence)
    out_root = ensure_private_dir(evidence / "independent_rerun")
    stamp = utc_now_iso_z().replace(":", "").replace("-", "")
    results: dict[str, Any] = {}
    ok = True
    for study in args.studies:
        runs = sorted((evidence / "runs").glob(f"{study}_run*"))
        if not runs:
            results[study] = {"error": "STUDY_RUN_MISSING"}
            ok = False
            continue
        row = rerun_study(runs[-1], study, out_root, stamp=stamp)
        results[study] = row
        if row["n_diffs"] or not row["contrasts_match_K7"]:
            ok = False
    report = {
        "schema": SCHEMA,
        "evidence": str(evidence),
        "results": results,
        "ok": ok,
        "generated_at_utc": utc_now_iso_z(),
        "report_sha256": "",
    }
    payload = json.dumps({k: v for k, v in report.items() if k != "report_sha256"},
                         ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode()
    report["report_sha256"] = hashlib.sha256(payload).hexdigest()
    target = Path(args.out) if args.out else out_root / "independent_rerun.json"
    atomic_write_text(target, json.dumps(report, ensure_ascii=False, indent=2,
                                         sort_keys=True) + "\n", mode=0o600)
    print(json.dumps({"ok": ok,
                      "studies": {s: {"n_diffs": r.get("n_diffs"),
                                      "contrasts_match_K7": r.get("contrasts_match_K7")}
                                  for s, r in results.items()},
                      "report_sha256": report["report_sha256"],
                      "path": str(target)}, ensure_ascii=False, indent=2))
    return 0 if ok else 4


if __name__ == "__main__":
    raise SystemExit(main())
