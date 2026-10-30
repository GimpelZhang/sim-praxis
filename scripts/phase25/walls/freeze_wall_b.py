#!/usr/bin/env python3
"""Task 5 墙 B 冻结脚本（0 付费，首呼前可幂等重跑）。

1. 从 run_5 工作目录只读复制冻结历史见证件 pitcher_open_watertight.obj，
   钉哈希/面数/水密/欧拉示性数；
2. 用仓内冻结评分器在宿主主 venv 上对 oracle + 六个 must-red 变异
   **重新实跑**，逐变异比对预定红项集合，结果落 validation/wall_b/；
3. lead-only 证据复制进冻结根（0700），登记 run_5 目录快照；
4. 双引擎密钥扫描后写冻结清单 wallB 段。

首呼锁存在即拒跑。
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.phase25.walls import freeze_common as fc
from scripts.phase25.walls.spike_backend import score_wall_b

LEAD_WALLB = Path("/tmp/t5lead/wallb")
RUN5_DIR = Path(
    "/mnt/artifacts/simpraxis/phase2/runs/task8_fluid/"
    "run_t8q5-fluid-stage1-20261027a_5")
FROZEN_OBJ_NAME = "pitcher_open_watertight.obj"
FROZEN_OBJ_SHA = (
    "ab30905766eff1309563f99469566e323e230bff449ba3f51d30bf230df2d4f8")
FROZEN_OBJ_SIZE = 48527

# 变异 → 预定失败检查项（冻结时刻重跑必须逐位相等；oracle 必须全绿）。
VARIANTS = [
    ("oracle", "oracle_solution.py", set()),
    ("mut1_counter_zero", "mut1_counter_zero.py",
     {"M2_open_shell_768_2304", "M2_nonmanifold_fan"}),
    ("mut2_solid_cube", "mut2_solid_cube.py", {"M1_vessel"}),
    ("mut3_sentinel_speed", "mut3_sentinel_speed.py",
     {"S0_no_sentinel_literals"}),
    ("mut4_garbage_file", "mut4_garbage_file.py", {"M1_vessel"}),
    ("mut5b_nan_passthrough", "mut5b_nan_passthrough.py",
     {"K5_nonfinite_raises"}),
    ("mut6_copy_historical", "mut6_copy_historical.py",
     {"S1_no_test_hardcoding", "M1_vessel", "K2_static",
      "K5_nonfinite_raises"}),
]
LEAD_LOGS = {
    "oracle": "oracle_run.log",
    "mut1_counter_zero": "mut1_counter_zero.log",
    "mut2_solid_cube": "mut2_solid_cube.log",
    "mut3_sentinel_speed": "mut3_sentinel_speed.log",
    "mut4_garbage_file": "mut4_garbage_file.log",
    "mut5b_nan_passthrough": "mut5b_nan_passthrough.log",
    "mut6_copy_historical": "mut6_copy_historical.log",
}


def ts_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def materialize_evidence(root: Path) -> dict:
    src = RUN5_DIR / FROZEN_OBJ_NAME
    if not src.is_file():
        raise SystemExit(f"run_5 历史见证件缺失：{src}")
    dst_dir = fc.ensure_dir(root / "evidence_wall_b")
    rec = fc.copy_readonly(src, dst_dir / FROZEN_OBJ_NAME)
    problems = []
    if rec["sha256"] != FROZEN_OBJ_SHA:
        problems.append(f"sha256 {rec['sha256']} != 钉死值")
    if rec["size"] != FROZEN_OBJ_SIZE:
        problems.append(f"size {rec['size']} != {FROZEN_OBJ_SIZE}")
    # 独立复检几何（主 venv trimesh）。
    import trimesh
    mesh = trimesh.load(rec["path"], force="mesh", process=False)
    geom = {
        "faces": int(len(mesh.faces)), "vertices": int(len(mesh.vertices)),
        "is_watertight": bool(mesh.is_watertight),
        "euler": int(mesh.euler_number),
        "bodies": int(mesh.split(only_watertight=False).__len__())
        if hasattr(mesh, "split") else None,
    }
    if (geom["faces"], geom["vertices"]) != (1536, 770):
        problems.append(f"面/点 {geom['faces']}/{geom['vertices']} != 1536/770")
    if not geom["is_watertight"]:
        problems.append("历史件非水密")
    if problems:
        raise SystemExit("冻结历史见证件复检失败：" + "; ".join(problems))
    return {**rec, "source": str(src), "geometry": geom}


def run_freeze_validation(root: Path, checker: Path,
                          frozen_obj: Path) -> tuple[Path, dict]:
    run_dir = fc.ensure_dir(root / "validation" / "wall_b")
    stamp = ts_compact()
    out_dir = fc.ensure_dir(run_dir / stamp)
    rows = []
    all_ok = True
    for label, src_name, expected_reds in VARIANTS:
        src = LEAD_WALLB / src_name
        if not src.is_file():
            raise SystemExit(f"lead 变异解缺失：{src}")
        work = out_dir / f"work_{label}"
        fc.ensure_dir(work)
        fc.atomic_write_bytes(work / "agent_solution.py", src.read_bytes())
        res = score_wall_b(work, checker, frozen_obj, timeout_s=180)
        score_files = sorted((work / ".score").glob("wall_b-*.json"))
        score = (json.loads(score_files[-1].read_text(encoding="utf-8"))
                 if score_files else None)
        checks = (score or {}).get("checks", [])
        reds = {c.get("check") for c in checks if not c.get("ok")}
        expect_rc = 0 if label == "oracle" else 1
        ok = (res.rc == expect_rc
              and (score or {}).get("verdict")
              == ("pass" if label == "oracle" else "fail")
              and reds == expected_reds)
        all_ok &= ok
        row = {"variant": label, "source": src_name, "rc": res.rc,
               "expected_rc": expect_rc,
               "verdict": (score or {}).get("verdict"),
               "red_checks": sorted(reds),
               "expected_red_checks": sorted(expected_reds),
               "ok": ok,
               "b_cap": (score or {}).get("b_cap"),
               "stdout_tail":
                   res.stdout.decode("utf-8", "replace")[-2000:],
               "stderr_tail":
                   res.stderr.decode("utf-8", "replace")[-2000:]}
        rows.append(row)
        fc.write_json(out_dir / f"{label}.json",
                      {k: v for k, v in row.items()
                       if k not in ("stdout_tail", "stderr_tail")})
        print(f"[freeze_wall_b] {label}: rc={res.rc} "
              f"reds={sorted(reds)} ok={ok}")
        # 删除守卫：仅删本脚本刚建的 0700 scratch。
        print(f"[freeze_wall_b] ls 后清理 scratch：{work} ->",
              sorted(p.name for p in work.iterdir()))
        shutil.rmtree(work)
    ledger = {
        "schema": "simpraxis.phase25.t5_wall_validation.v1",
        "wall": "wall_b", "ts_iso": fc.utc_now(), "run_dir": stamp,
        "all_ok": all_ok, "rows": rows,
    }
    fc.write_json(out_dir / "ledger.json", ledger)
    fc.write_json(run_dir / "ledger.json",
                  {"schema": ledger["schema"], "wall": "wall_b",
                   "ts_iso": ledger["ts_iso"], "run_dir": stamp,
                   "all_ok": all_ok,
                   "variants": [{"variant": r["variant"], "ok": r["ok"],
                                 "red_checks": r["red_checks"]}
                                for r in rows]})
    if not all_ok:
        raise SystemExit(
            "墙 B 冻结时刻变异验证未全对（红项集合有偏差，fail-closed）：\n"
            + json.dumps([{"variant": r["variant"], "ok": r["ok"],
                           "expected": r["expected_red_checks"],
                           "observed": r["red_checks"]} for r in rows
                          if not r["ok"]], indent=1, ensure_ascii=False))
    return out_dir, ledger


def copy_lead_evidence(root: Path) -> list[dict]:
    dst_dir = fc.ensure_dir(root / "lead_only" / "wall_b")
    records = []
    names = [src for _label, src, _reds in VARIANTS]
    names += list(LEAD_LOGS.values())
    names.append("vessel_oracle.obj")
    for name in names:
        src = LEAD_WALLB / name
        if not src.is_file():
            raise SystemExit(f"lead 证据缺失：{src}")
        records.append(fc.copy_readonly(src, dst_dir / name))
    return records


def main() -> int:
    root = fc.freeze_root()
    fc.assert_unlocked(root)
    for d in ("evidence_wall_b", "manifests", "snapshots",
              "validation", "lead_only"):
        fc.ensure_dir(root / d)
    print(f"[freeze_wall_b] 冻结根：{root}")
    evidence = materialize_evidence(root)
    print("[freeze_wall_b] 历史见证件复检通过：1536/770 watertight")
    checker = fc.GATES_DIR / "wall_b" / "wall_b_checker.py"
    frozen_obj = root / "evidence_wall_b" / FROZEN_OBJ_NAME
    out_dir, ledger = run_freeze_validation(root, checker, frozen_obj)
    print(f"[freeze_wall_b] oracle+6 变异验证通过：{out_dir.name}")
    lead_records = copy_lead_evidence(root)
    snap = fc.register_tree_hashes(RUN5_DIR, ".")
    snap["provenance"] = ("Task8 run_5 机判战役工作目录；OBJ 复制进 "
                          "evidence_wall_b/，其余仅登记不复制")
    fc.write_snapshot(root, "run5_workdir", {"snapshot": snap})
    scan_paths = [
        *(fc.GATES_DIR / f"wall_b/{n}" for n in fc.GATE_FILES["wall_b"]),
        *(fc.GATES_DIR / n for n in fc.GATE_FILES["common"]),
        *[Path(r["path"]) for r in lead_records
          if not r["path"].endswith(".obj")],
        RUN5_DIR / "genesis_fluid_pour.py",
        RUN5_DIR / "failure.json",
        RUN5_DIR / "FAILURE.json",
    ]
    fc.write_secret_report(
        root, scanned_paths=scan_paths,
        scope="wall_b freeze (gate files + lead + run5 text artifacts)")
    fc.merge_section(root, "wall_b", {
        "frozen_evidence": {
            "path": str(root / "evidence_wall_b" / FROZEN_OBJ_NAME),
            "sha256": evidence["sha256"], "size": evidence["size"],
            "source": evidence["source"], "geometry": evidence["geometry"]},
        "snapshots": ["run5_workdir.json"],
        "lead_only": lead_records,
        "validation_dir": str(out_dir.relative_to(root)),
        "validation_all_ok": ledger["all_ok"],
        "b_cap_oracle": next(r["b_cap"] for r in ledger["rows"]
                             if r["variant"] == "oracle"),
        "secret_scan": "snapshots/secret_scan.json",
    })
    print("[freeze_wall_b] 完成，wallB 段已合入 freeze_manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
