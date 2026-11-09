#!/usr/bin/env python3
"""AC17 取证：独立 oracle 的 import 审计 + 真实/边界序列双算法对照。

零容器、零付费、零网络：
  * import 审计用 AST 静态检查——oracle 只许 import schemas 常量，禁止触达
    ``evaluator`` 的沉降/效应/判决函数；
  * 真实序列取自 campaign 里 R 族参考 trial 的可信观测（原样读，不重算物理）；
  * 边界序列现场构造（恰等阈值、末点越界、全删失、t_low=0），两算法逐项比对；
  * 预锁界 = ``independent_oracle.TOL``（1e-12 相对）。

产物：``<campaign>/analysis/t3_oracle/audit.json``。
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (_REPO_ROOT, _REPO_ROOT / "scripts" / "rsi_r01"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import independent_oracle as ORACLE  # noqa: E402
from orchestrator.common import runtime_paths as RP  # noqa: E402
from orchestrator.rsi_r01 import evaluator as EV  # noqa: E402
from orchestrator.rsi_r01 import schemas as S  # noqa: E402

SCHEMA = "simpraxis.rsi_r01.oracle_audit.v1"
_ALLOWED_PLAIN = {"__future__", "argparse", "json", "sys", "pathlib", "typing"}
_ALLOWED_FROM = {"orchestrator.rsi_r01.schemas"}


def import_audit(path: Path) -> dict[str, Any]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            # 记录**解析后**的目标：``from a.b import c`` ⇒ ``a.b.c``；
            # 只允许 ``orchestrator.rsi_r01.schemas`` 这一条引擎依赖。
            imports.extend(f"{module}.{a.name}" for a in node.names)
    unique = sorted(set(imports))
    disallowed = [m for m in unique
                  if m not in _ALLOWED_FROM and m.split(".")[0] not in _ALLOWED_PLAIN]
    touches_evaluator = any("evaluator" in m for m in unique)
    return {"file": str(path), "imports": unique, "disallowed": disallowed,
            "touches_evaluator": touches_evaluator,
            "ok": not disallowed and not touches_evaluator}


def _real_observations(campaign: Path, trial_id: str) -> dict[str, dict[str, Any]]:
    runs = []
    with (campaign / "trials" / trial_id / "runs.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                runs.append(json.loads(line))
    latest: dict[str, str] = {}
    for run in runs:
        cell = run.get("cell")
        if cell:
            latest[cell] = run["manifest"]["run_id"]
    observations = {}
    for cell, run_id in latest.items():
        doc = json.loads((campaign / "observations" / "trusted" / trial_id
                          / f"{run_id}.json").read_text(encoding="utf-8"))
        observations[cell] = doc
    return observations


def _boundary_cases() -> dict[str, dict[str, dict[str, Any]]]:
    n = S.EXPECTED_ROWS
    t = [i * S.DT_S for i in range(n)]
    serial = {"next": 0}

    def doc(theta: list[float], omega: list[float]) -> dict[str, Any]:
        serial["next"] += 1
        return {"schema": S.SCHEMA_OBSERVATION, "kind": S.OBS_KIND_PENDULUM,
                "run_id": f"synthetic_boundary_{serial['next']}",
                "trial_id": "synthetic", "instance_id": "R-dev",
                "params": {"damping": 0.15, "initial_angle": 0.0,
                           "timestep": S.DT_S, "steps": S.N_STEPS},
                "t": t, "theta": theta, "omega": omega}

    quiet = doc([0.0] * n, [0.0] * n)
    exact = doc([S.THETA_BAND] * n, [0.0] * n)          # 恰等阈值：不入带
    moving = doc([0.5] * n, [0.0] * n)                  # 全删失
    tail_out = doc([0.0] * (n - 1) + [0.5], [0.0] * n)  # 仅末点越界
    return {
        "exact_threshold": {f"{d}/{a}": exact for d in ("low", "high")
                            for a in ("plus", "minus")},
        "all_censored": {f"{d}/{a}": moving for d in ("low", "high")
                         for a in ("plus", "minus")},
        "tail_out": {"low/plus": quiet, "low/minus": quiet,
                     "high/plus": tail_out, "high/minus": quiet},
    }


def _compare_pair(observations: dict[str, Any], h: int) -> dict[str, Any]:
    judge = EV.judge_research(observations=observations, h=h).as_dict()
    oracle = ORACLE.judge_from_observations(observations, h)
    problems = ORACLE.compare(judge, oracle)
    return {"judge_claim": judge["claim_status"],
            "oracle_claim": oracle["claim_status"],
            "judge_mean": judge["mean_effect"], "oracle_mean": oracle["mean_effect"],
            "diff": problems, "ok": not problems}


def run(*, campaign_root: Path) -> dict[str, Any]:
    audit = import_audit(_REPO_ROOT / "scripts" / "rsi_r01" / "independent_oracle.py")
    real: list[dict[str, Any]] = []
    for trial in sorted((campaign_root / "trials").glob("ref_R_*")):
        obs = _real_observations(campaign_root, trial.name)
        if len(obs) != 4:
            real.append({"trial_id": trial.name, "ok": False,
                         "reason": f"cells={sorted(obs)}"})
            continue
        pair = _compare_pair(obs, 1)
        pair.update({"trial_id": trial.name})
        real.append(pair)
    boundary: dict[str, Any] = {}
    for name, obs in _boundary_cases().items():
        boundary[name] = _compare_pair(obs, 1)
    low_zero = {"judge": EV.effect(0.0, 1.0), "oracle": ORACLE.effect_of(0.0, 1.0)}
    low_zero["ok"] = (low_zero["judge"]["value"] is None
                      and low_zero["oracle"] is None)
    out = {"schema": SCHEMA, "campaign_id": campaign_root.name,
           "created_utc": S.utc_now(), "tol": ORACLE.TOL,
           "import_audit": audit, "real_sequences": real,
           "boundary_sequences": boundary, "t_low_zero": low_zero}
    checks = [audit["ok"], all(r["ok"] for r in real),
              all(b["ok"] for b in boundary.values()), low_zero["ok"]]
    out["verdict"] = "PASS" if all(checks) else "FAIL"
    out["checks"] = {"import_audit": audit["ok"],
                     "real_sequences": all(r["ok"] for r in real),
                     "boundary_sequences": all(b["ok"] for b in boundary.values()),
                     "t_low_zero": low_zero["ok"]}
    target_dir = campaign_root / "analysis" / "t3_oracle"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "audit.json"
    S.write_canonical_json(target, out)
    return {"path": str(target), "checks": out["checks"], "verdict": out["verdict"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RSI-R0.1 AC17 oracle 独立性审计")
    ap.add_argument("--campaign-root", default=None)
    args = ap.parse_args(argv)
    root = Path(args.campaign_root) if args.campaign_root else RP.rsi_r01_campaign_root()
    if not root.is_dir():
        raise SystemExit(f"campaign 根不存在：{root}")
    out = run(campaign_root=root)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if out["verdict"] == "PASS" else 3


if __name__ == "__main__":
    sys.exit(main())
