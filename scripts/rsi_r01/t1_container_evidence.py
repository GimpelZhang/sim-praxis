#!/usr/bin/env python3
"""T4 取证：AC05–AC09 的真实容器证据（零模型调用）。

每项都走**真实容器**与生产同一条代码路径（``TrialObservationStore`` /
``AnalysisRunner``），不做 mock 冒名。产物：``<campaign>/analysis/t1_container/
evidence.json``，逐项 expected/observed/verdict。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from orchestrator.common import runtime_paths as RP
from orchestrator.rsi_r01 import analysis_runner as AR
from orchestrator.rsi_r01 import observations as O
from orchestrator.rsi_r01 import schemas as S
from orchestrator.rsi_r01 import task_contract as TC

SCHEMA = "simpraxis.rsi_r01.t1_container_evidence.v1"


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _err(fn) -> dict[str, Any]:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 —— 记录类型化拒绝
        return {"raised": type(exc).__name__, "message": str(exc)[:240]}
    return {"raised": None, "message": None}


def ac05_ac06(store: O.TrialObservationStore, scene: str) -> dict[str, Any]:
    cases = []
    for damping in (0.10, 0.25, 0.45):
        req = O.ExperimentRequest.pendulum(
            trial_id="t1_ac05", instance_id="R-dev", scene_xml=scene,
            damping=damping, initial_angle=0.6, steps=S.N_STEPS)
        manifest, doc = store.run(req)
        trusted = store.trusted_dir / f"{manifest['run_id']}.json"
        finite = (all(math.isfinite(v) for v in doc["theta"])
                  and all(math.isfinite(v) for v in doc["omega"]))
        cases.append({
            "requested": {"damping": damping, "initial_angle": 0.6},
            "applied": manifest["applied"],
            "applied_equals_requested": abs(
                float(manifest["applied"]["damping"]) - damping) < 1e-12,
            "manifest_params": manifest["params"],
            "run_id": manifest["run_id"],
            "scene_sha256": manifest["scene_sha256"],
            "rows": len(doc["t"]),
            "manifest_rows": manifest["rows"],
            "first_point": {"t": doc["t"][0], "theta": doc["theta"][0],
                            "omega": doc["omega"][0]},
            "last_point": {"t": doc["t"][-1], "theta": doc["theta"][-1],
                           "omega": doc["omega"][-1]},
            "finite": finite,
            "host_sha256": _sha(trusted),
            "manifest_sha256": manifest["observation_sha256"],
            "hash_match": _sha(trusted) == manifest["observation_sha256"],
            "t_end_expected_s": S.N_STEPS * S.DT_S,
            "t_end_ok": abs(doc["t"][-1] - S.N_STEPS * S.DT_S) < 1e-9,
            "settle_time_s": _settle(doc),
        })
    dampings = [c["requested"]["damping"] for c in cases]
    applied = [float(c["applied"]["damping"]) for c in cases]
    return {
        "AC05": {
            "cases": cases,
            "distinct_requests": len(set(dampings)),
            "applied_matches_all": all(c["applied_equals_requested"] for c in cases),
            "physical_response_varies": len({round(c["settle_time_s"], 6)
                                             for c in cases if c["settle_time_s"]}) > 1,
            "verdict": "PASS" if (len(set(dampings)) == 3
                                  and all(c["applied_equals_requested"] for c in cases)
                                  and applied == dampings) else "FAIL",
        },
        "AC06": {
            "all_rows_2001": all(c["rows"] == S.EXPECTED_ROWS for c in cases),
            "all_t_end_ok": all(c["t_end_ok"] for c in cases),
            "first_point_t_zero": all(abs(c["first_point"]["t"]) < 1e-12 for c in cases),
            "all_finite": all(c["finite"] for c in cases),
            "all_hash_match": all(c["hash_match"] for c in cases),
            "verdict": "PASS" if all([
                all(c["rows"] == S.EXPECTED_ROWS for c in cases),
                all(c["t_end_ok"] for c in cases),
                all(abs(c["first_point"]["t"]) < 1e-12 for c in cases),
                all(c["finite"] for c in cases),
                all(c["hash_match"] for c in cases)]) else "FAIL",
        },
    }


def _settle(doc: dict[str, Any]) -> float | None:
    for i in range(len(doc["t"]) - 1, -1, -1):
        if not (abs(doc["theta"][i]) < S.THETA_BAND
                and abs(doc["omega"][i]) < S.OMEGA_BAND):
            return float(doc["t"][i + 1]) if i + 1 < len(doc["t"]) else None
    return 0.0


def ac07(store: O.TrialObservationStore, manifest: dict[str, Any]) -> dict[str, Any]:
    rid = manifest["run_id"]
    cases: dict[str, Any] = {}
    cases["cross_trial"] = _err(lambda: store.load_trusted(
        rid, manifest, expected_trial="another_trial",
        expected_instance="R-dev"))
    cases["cross_instance"] = _err(lambda: store.load_trusted(
        rid, manifest, expected_trial="t1_ac05", expected_instance="R-val-1"))
    cases["unknown_run"] = _err(lambda: store.load_trusted(
        "run_00000000000000000000000000000000", manifest,
        expected_trial="t1_ac05", expected_instance="R-dev"))

    scratch = store.trusted_dir.parent / "ac07_scratch"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    tampered = scratch / f"{rid}.json"
    blob = bytearray((store.trusted_dir / f"{rid}.json").read_bytes())
    blob[len(blob) // 2] ^= 0x01
    tampered.write_bytes(bytes(blob))
    tampered_store = O.TrialObservationStore(trusted_dir=scratch,
                                             public_dir=scratch / "public")
    cases["tampered_bytes"] = _err(lambda: tampered_store.load_trusted(
        rid, manifest, expected_trial="t1_ac05", expected_instance="R-dev"))

    short = scratch / "short"
    short.mkdir()
    doc = json.loads((store.trusted_dir / f"{rid}.json").read_text(encoding="utf-8"))
    doc["t"] = doc["t"][:-1]
    for key in ("theta", "omega", "x", "y", "vx", "vy"):
        if key in doc:
            doc[key] = doc[key][:-1]
    (short / f"{rid}.json").write_text(json.dumps(doc), encoding="utf-8")
    short_store = O.TrialObservationStore(trusted_dir=short,
                                          public_dir=short / "public")
    cases["missing_last_point"] = _err(lambda: short_store.load_trusted(
        rid, manifest, expected_trial="t1_ac05", expected_instance="R-dev"))

    rejected = {k: bool(v["raised"]) for k, v in cases.items()}
    return {"cases": cases, "all_rejected": all(rejected.values()),
            "verdict": "PASS" if all(rejected.values()) else "FAIL"}


_PROBES: list[tuple[str, str, int]] = [
    ("baseline_ok", '''\
import glob, json
docs = [json.load(open(p)) for p in sorted(glob.glob("/data/run_*.json"))]
out = {"runs": len(docs), "kind": "probe_baseline"}
json.dump(out, open("/out/analysis.json", "w"))
print("rows", len(docs))
''', 30),
    ("no_network", '''\
import json, socket
blocked = False
try:
    socket.create_connection(("1.1.1.1", 53), timeout=2).close()
except Exception as exc:
    blocked = True
    err = type(exc).__name__
json.dump({"network_blocked": blocked, "error": err if blocked else None},
          open("/out/analysis.json", "w"))
''', 30),
    ("host_path_invisible", '''\
import json, os
blocked = True
try:
    open("/home/vipuser/sim-praxis/CLAUDE.md").read()
    blocked = False
except Exception as exc:
    err = type(exc).__name__
json.dump({"host_path_blocked": blocked, "error": err if blocked else None,
           "data_entries": sorted(os.listdir("/data"))[:3]},
          open("/out/analysis.json", "w"))
''', 30),
    ("traversal", '''\
import json
blocked = True
try:
    open("/data/../../home/vipuser/CLAUDE.md").read()
    blocked = False
except Exception as exc:
    err = type(exc).__name__
json.dump({"traversal_blocked": blocked, "error": err if blocked else None},
          open("/out/analysis.json", "w"))
''', 30),
    ("symlink_output", '''\
import glob, os
src = sorted(glob.glob("/data/run_*.json"))[0]
os.symlink(src, "/out/analysis.json")
''', 30),
    ("unexpected_output", '''\
open("/out/evil.txt", "w").write("x")
open("/out/analysis.json", "w").write("{}")
''', 30),
    ("output_over_cap", '''\
open("/out/analysis.json", "w").write("x" * 100000)
''', 30),
    ("stream_over_cap", '''\
print("y" * 20000)
open("/out/analysis.json", "w").write("{}")
''', 30),
    ("timeout", '''\
import time
time.sleep(60)
''', 3),
]


def _probe_output(runner: AR.AnalysisRunner, analysis_id: str) -> Any:
    path = runner.trial_dir / "analysis" / analysis_id / "out" / "analysis.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def ac08(runner: AR.AnalysisRunner, run_ids: list[str]) -> dict[str, Any]:
    results = []
    for name, source, timeout in _PROBES:
        ev = runner.run(source, run_ids, timeout_s=timeout)
        results.append({
            "probe": name,
            "analysis_id": ev.analysis_id,
            "source_sha256": ev.source_sha256,
            "host_source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "exit_code": ev.exit_code, "timed_out": ev.timed_out,
            "violations": ev.violations,
            "outputs": ev.outputs,
            "stderr_preview": ev.stderr_preview[:200],
            "stdout_preview": ev.stdout_preview[:200],
            "seconds": ev.seconds,
        })
        results[-1]["output_doc"] = _probe_output(runner, ev.analysis_id)
    got = {r["probe"]: r for r in results}
    policy_runner = AR.AnalysisRunner(trial_dir=runner.trial_dir.parent / "cap_trial",
                                      data_dir=runner.data_dir, max_analyses=1)
    policy_runner.run('open("/out/analysis.json","w").write("{}")', run_ids)
    cap = _err(lambda: policy_runner.run(
        'open("/out/analysis.json","w").write("{}")', run_ids))
    too_big = _err(lambda: runner.run("#" * (S.ANALYSIS_MAX_SOURCE_BYTES + 10),
                                      run_ids))
    net_doc = got["no_network"].get("output_doc") or {}
    host_doc = got["host_path_invisible"].get("output_doc") or {}
    trav_doc = got["traversal"].get("output_doc") or {}
    checks = {
        "baseline_ran": got["baseline_ok"]["exit_code"] == 0,
        "no_network": bool(net_doc.get("network_blocked")) and
        got["no_network"]["exit_code"] == 0,
        "host_path_invisible": bool(host_doc.get("host_path_blocked")) and
        got["host_path_invisible"]["exit_code"] == 0,
        "traversal": bool(trav_doc.get("traversal_blocked")) and
        got["traversal"]["exit_code"] == 0,
        "symlink_rejected": any(v.startswith("SYMLINK_OUTPUT")
                                for v in got["symlink_output"]["violations"]),
        "unexpected_output_rejected": any(
            v.startswith("UNEXPECTED_OUTPUT")
            for v in got["unexpected_output"]["violations"]),
        "output_over_cap_rejected": any(
            v.startswith("OUTPUT_OVER_CAP")
            for v in got["output_over_cap"]["violations"]),
        "stream_over_cap_rejected": "STREAM_OVER_CAP"
        in got["stream_over_cap"]["violations"],
        "timeout_enforced": got["timeout"]["timed_out"] is True,
        "analysis_cap_rejected": bool(cap["raised"]),
        "source_over_cap_rejected": bool(too_big["raised"]),
        "source_hash_recorded": all(
            r["source_sha256"] == r["host_source_sha256"] for r in results),
    }
    pids = _container_pids_limit(runner)
    checks["pids_limit_enforced"] = bool(
        pids.get("event_ok") is True
        and str((pids.get("observed") or {}).get("pids_max")) == str(S.ANALYSIS_PIDS))
    return {"probes": results, "analysis_cap": cap, "source_over_cap": too_big,
            "pids_limit_inside_container": pids, "checks": checks,
            "verdict": "PASS" if all(checks.values()) else "FAIL"}


def _container_pids_limit(runner: AR.AnalysisRunner) -> dict[str, Any]:
    ev = runner.run('''\
import json, os
val = None
for p in ("/sys/fs/cgroup/pids.max", "/sys/fs/cgroup/pids/pids.max"):
    try:
        val = open(p).read().strip()
        break
    except OSError:
        continue
json.dump({"pids_max": val}, open("/out/analysis.json", "w"))
''', sorted(p.name[:-5] for p in runner.data_dir.glob("run_*.json"))[:1])
    out = None
    path = runner.trial_dir / "analysis" / ev.analysis_id / "out" / "analysis.json"
    if path.is_file():
        out = json.loads(path.read_text(encoding="utf-8"))
    return {"event_ok": not ev.violations, "observed": out}


def ac09(runner: AR.AnalysisRunner, campaign_root: Path) -> dict[str, Any]:
    ref = campaign_root / "analysis" / "reference_actor" / "R-dev.json"
    record = json.loads(ref.read_text(encoding="utf-8")) if ref.is_file() else {}
    acts = [a for a in record.get("actions", []) if isinstance(a, dict)]
    analyze = [a for a in acts if a.get("action") == "analyze"]
    event = (analyze[-1].get("analysis") if analyze else {}) or {}
    trial_dir = campaign_root / "trials" / str(record.get("trial_id") or "ref_R_dev")
    # 提交原件（含 analysis_ids/run_ids）只在 trial 的 submissions.jsonl，
    # 动作事件流按设计不携带这些负载字段。
    subs_path = trial_dir / "submissions.jsonl"
    subs = ([json.loads(line) for line in subs_path.read_text(encoding="utf-8").splitlines()
             if line.strip()] if subs_path.is_file() else [])
    last_sub = subs[-1] if subs else {}
    sub = last_sub.get("analysis_ids") or []
    aid = str(event.get("analysis_id") or "")
    out_path = trial_dir / "analysis" / aid / "out" / "analysis.json"
    outputs_ok = bool(out_path.is_file() and event.get("outputs")
                      and event["outputs"][0]["sha256"] == _sha(out_path))
    checks = {
        "analysis_executed_in_container": bool(event.get("analysis_id"))
        and "exit_code" in event,
        "source_hash_recorded": len(str(event.get("source_sha256") or "")) == 64,
        "output_hash_matches_host": outputs_ok,
        "submit_references_analysis": bool(aid) and last_sub.get("ok") is True
        and sub == [aid],
        "submit_uses_same_runs": sorted(last_sub.get("run_ids") or [])
        == sorted(event.get("run_ids") or []),
        "not_prefilled_answers": "+" not in aid and aid.startswith("a"),
    }
    return {"evidence": {"analysis_event": event,
                         "submission": last_sub,
                         "output_file": str(out_path)},
            "checks": checks,
            "verdict": "PASS" if all(checks.values()) else "FAIL"}


def run(*, campaign_root: Path) -> dict[str, Any]:
    work = campaign_root / "analysis" / "t1_container"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    store = O.TrialObservationStore(trusted_dir=work / "trusted",
                                    public_dir=work / "public")
    inst = TC.get("R-dev")
    scene = inst.scene_xml()
    first = ac05_ac06(store, scene)
    manifest = json.loads(next(store.trusted_dir.glob("*.manifest.json"))
                          .read_text(encoding="utf-8"))
    seven = ac07(store, manifest)
    runner = AR.AnalysisRunner(trial_dir=work / "analysis_trial",
                               data_dir=store.public_dir, max_analyses=16)
    eight = ac08(runner, [manifest["run_id"]])
    nine = ac09(runner, campaign_root)
    out = {"schema": SCHEMA, "campaign_id": campaign_root.name,
           "created_utc": S.utc_now(), "container_runs": len(
               list(store.trusted_dir.glob("*.manifest.json")))
           + len(runner.events),
           "AC05": first["AC05"], "AC06": first["AC06"], "AC07": seven,
           "AC08": eight, "AC09": nine}
    out["verdicts"] = {k: out[k]["verdict"] for k in
                       ("AC05", "AC06", "AC07", "AC08", "AC09")}
    out["all_pass"] = all(v == "PASS" for v in out["verdicts"].values())
    target = work / "evidence.json"
    S.write_canonical_json(target, out)
    return {"path": str(target), "verdicts": out["verdicts"],
            "all_pass": out["all_pass"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RSI-R0.1 T1/T4 容器证据（零模型调用）")
    ap.add_argument("--campaign-root", default=None)
    args = ap.parse_args(argv)
    root = (Path(args.campaign_root) if args.campaign_root
            else RP.rsi_r01_campaign_root())
    if not root.is_dir():
        raise SystemExit(f"campaign 根不存在：{root}")
    out = run(campaign_root=root)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if out["all_pass"] else 3


if __name__ == "__main__":
    sys.exit(main())
