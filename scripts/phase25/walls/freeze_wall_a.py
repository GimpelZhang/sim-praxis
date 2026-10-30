#!/usr/bin/env python3
"""Task 5 墙 A 冻结脚本（0 付费，首呼前可幂等重跑）。

做四件事：
1. 物化 ``$SIMPRAXIS_T5_FREEZE_ROOT/gate_env``（libero_cfg + 容器内才解析的
   pyshim/libero 悬空软链）；
2. 用仓内冻结评分器 + spike 后端在一次性兄弟容器里**重新实跑** must-green/
   must-red 双向验证（绿 12/12 pass/rc0，红 EGL 崩溃 fail/rc1），结果落
   validation/wall_a/；
3. lead-only 证据（参考解/红变异/日志）复制进冻结根（0700，永不挂模型容器），
   登记 LIBERO tour / 隔离 venv / 主 venv 三个快照（只登记不复制）；
4. 双引擎密钥扫描后写冻结清单 wallA 段。

首呼锁（manifests/LOCK.json）存在即拒跑。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.phase25.walls import freeze_common as fc
from scripts.phase25.walls.spike_backend import (
    IMAGE_ID, IMAGE_REPODIGEST, score_wall_a,
)

LEAD = Path("/tmp/t5lead")
LEAD_WALLA = LEAD / "walla"
LEAD_GATE_ENV = LEAD / "gate_env"

# lead-only 复制清单（参考解/红变异/跑法/权威日志；陈旧的 green_score.log
# 是修评分器前的 fail 记录，不进冻结面，以本次重跑为准）。
LEAD_COPIES = [
    "green_build_env.py",
    "red_build_env_render.py",
    "run_both.sh",
    "run_both2.log",
]


def ts_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def check_image() -> None:
    proc = subprocess.run(
        ["docker", "inspect", IMAGE_ID, "--format",
         "{{json .RepoDigests}}|{{json .RepoTags}}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if proc.returncode != 0:
        raise SystemExit(f"冻结镜像不存在：{IMAGE_ID}\n"
                         + proc.stderr.decode("utf-8", "replace"))
    out = proc.stdout.decode("utf-8", "replace")
    if IMAGE_REPODIGEST.split(":", 1)[1] not in out:
        raise SystemExit(f"镜像 RepoDigest 不符：{out}")
    if "sim-praxis/agentic:phase25-t4" not in out:
        raise SystemExit(f"镜像 tag 不符：{out}")


def materialize_gate_env(root: Path) -> dict:
    gate_env = fc.ensure_dir(root / "gate_env")
    cfg_src = LEAD_GATE_ENV / "libero_cfg" / "config.yaml"
    if not cfg_src.is_file():
        raise SystemExit(f"lead gate_env config 缺失：{cfg_src}")
    cfg_dst = gate_env / "libero_cfg" / "config.yaml"
    cfg_rec = fc.copy_readonly(cfg_src, cfg_dst)
    # 悬空软链：宿主上目标不存在，容器内挂 tour 后解析到 /opt/libero_tour/libero
    fc.make_symlink(gate_env / "pyshim" / "libero", "/opt/libero_tour/libero")
    os.chmod(gate_env / "pyshim", 0o700)
    os.chmod(gate_env / "libero_cfg", 0o700)
    return {"libero_cfg/config.yaml": cfg_rec,
            "pyshim_libero_target": "/opt/libero_tour/libero",
            "pyshim_libero_dangling_on_host": True}


def _fresh_score(gate_dir: Path, root: Path, work: Path,
                 variant_src: Path) -> dict:
    fc.ensure_dir(work)
    fc.atomic_write_bytes(work / "agent_solution.py",
                          variant_src.read_bytes())
    res = score_wall_a(work, gate_dir, root, timeout_s=240)
    score_files = sorted((work / ".score").glob("wall_a-*.json"))
    score_json = None
    if score_files:
        score_json = json.loads(score_files[-1].read_text(encoding="utf-8"))
    return {"rc": res.rc, "timed_out": res.timed_out,
            "stdout_tail": res.stdout.decode("utf-8", "replace")[-4000:],
            "stderr_tail": res.stderr.decode("utf-8", "replace")[-4000:],
            "score": score_json,
            "score_file": score_files[-1].name if score_files else None}


def run_freeze_validation(root: Path, gate_dir: Path) -> tuple[Path, dict]:
    run_dir = fc.ensure_dir(root / "validation" / "wall_a")
    stamp = ts_compact()
    out_dir = fc.ensure_dir(run_dir / stamp)
    # 临时 workdir 放冻结根内（0700），评分后只留分数与日志，解本体在 lead_only。
    results: dict[str, dict] = {}
    scratch_dirs: list[Path] = []
    for variant, src_name in (("green", "green_build_env.py"),
                              ("red", "red_build_env_render.py")):
        src = LEAD_WALLA / src_name
        if not src.is_file():
            raise SystemExit(f"lead 双向验证解缺失：{src}")
        work = out_dir / f"work_{variant}"
        scratch_dirs.append(work)
        r = _fresh_score(gate_dir, root, work, src)
        results[variant] = r
        fc.atomic_write_bytes(
            out_dir / f"{variant}_stdout.log",
            r["stdout_tail"].encode("utf-8"))
        fc.atomic_write_bytes(
            out_dir / f"{variant}_stderr.log",
            r["stderr_tail"].encode("utf-8"))

    g, r = results["green"], results["red"]
    g_score, r_score = g["score"], r["score"]
    checks = (g_score or {}).get("checks", [])
    assertions = {
        "green_rc0": g["rc"] == 0,
        "green_verdict_pass": (g_score or {}).get("verdict") == "pass",
        "green_12_of_12": len(checks) == 12
        and all(c.get("ok") for c in checks),
        "red_rc1": r["rc"] == 1,
        "red_verdict_fail": (r_score or {}).get("verdict") == "fail",
        "red_egl_error": "create_initialized_egl_device_display"
        in (r["stdout_tail"] + r["stderr_tail"]),
    }
    ledger = {
        "schema": "simpraxis.phase25.t5_wall_validation.v1",
        "wall": "wall_a", "ts_iso": fc.utc_now(), "run_dir": stamp,
        "assertions": assertions,
        "green": {"rc": g["rc"], "score_file": g["score_file"],
                  "elapsed_s": (g_score or {}).get("elapsed_s")},
        "red": {"rc": r["rc"], "score_file": r["score_file"],
                "elapsed_s": (r_score or {}).get("elapsed_s"),
                "traceback_head": ((r_score or {}).get("traceback") or "")
                .splitlines()[-1:]},
    }
    # 删除守卫：仅删本脚本刚建的 0700 scratch（ls 留痕后），不动其他路径。
    for w in scratch_dirs:
        print(f"[freeze_wall_a] ls 后清理 scratch：{w} ->",
              sorted(p.name for p in w.iterdir()))
        shutil.rmtree(w)
    fc.write_json(out_dir / "ledger.json", ledger)
    if not all(assertions.values()):
        raise SystemExit("墙 A 冻结时刻双向验证未全绿：\n"
                         + json.dumps(assertions, indent=1))
    summary = {k: v for k, v in ledger.items() if k in
               ("schema", "wall", "ts_iso", "run_dir", "assertions")}
    fc.write_json(run_dir / "ledger.json", summary)
    return out_dir, ledger


def copy_lead_evidence(root: Path) -> list[dict]:
    dst_dir = fc.ensure_dir(root / "lead_only" / "wall_a")
    records = []
    for name in LEAD_COPIES:
        src = LEAD_WALLA / name
        if not src.is_file():
            raise SystemExit(f"lead 证据缺失：{src}")
        records.append(fc.copy_readonly(src, dst_dir / name))
    return records


def register_snapshots(root: Path) -> list[str]:
    written = []
    from scripts.phase25.walls.spike_backend import (
        LIBERO_TOUR, VENV_LIBERO, MAIN_VENV)
    tour_path = Path(LIBERO_TOUR)
    tour = fc.register_git_worktree(tour_path)
    tour["libero_subtree"] = fc.register_tree_hashes(
        tour_path, "libero", limit=4096)
    tour["note"] = ("untracked .write_probe 与 libero/__init__.py 在 porcelain "
                    "与 libero_subtree 条目中按磁盘实字节登记；模型容器挂载的"
                    "是此 worktree 实况")
    p = fc.write_snapshot(root, "libero_tour", {"snapshot": tour})
    written.append(p.name)

    p = fc.write_snapshot(root, "venv_libero",
                          {"snapshot": fc.register_venv(Path(VENV_LIBERO))})
    written.append(p.name)

    main_ver = subprocess.run(
        [f"{MAIN_VENV}/bin/python", "-c",
         "import sys,platform;print(sys.version.split()[0],platform.machine())"],
        stdout=subprocess.PIPE, timeout=30).stdout.decode().strip()
    p = fc.write_snapshot(root, "main_venv", {"snapshot": {
        "root": MAIN_VENV,
        "python_realpath": str(Path(os.path.realpath(f"{MAIN_VENV}/bin/python"))),
        "version": main_ver,
        "mount_purpose": "隔离 venv 的 python 是绝对软链，须以同宿主路径挂载"}})
    written.append(p.name)
    return written


def main() -> int:
    root = fc.freeze_root()
    fc.assert_unlocked(root)
    for d in ("gate_env", "evidence_wall_b", "manifests", "snapshots",
              "validation", "lead_only"):
        fc.ensure_dir(root / d)
    print(f"[freeze_wall_a] 冻结根：{root}")
    check_image()
    gate_env_rec = materialize_gate_env(root)
    gate_dir = fc.GATES_DIR / "wall_a"
    out_dir, ledger = run_freeze_validation(root, gate_dir)
    print(f"[freeze_wall_a] 双向验证通过：{out_dir.name}")
    lead_records = copy_lead_evidence(root)
    snapshots = register_snapshots(root)
    scan_paths = [
        *(fc.GATES_DIR / f"wall_a/{n}" for n in fc.GATE_FILES["wall_a"]),
        *(fc.GATES_DIR / n for n in fc.GATE_FILES["common"]),
        root / "gate_env" / "libero_cfg" / "config.yaml",
        *[Path(r["path"]) for r in lead_records],
    ]
    fc.write_secret_report(root, scanned_paths=scan_paths,
                           scope="wall_a freeze (gate files + gate_env + lead)")
    fc.merge_section(root, "wall_a", {
        "gate_env": gate_env_rec,
        "snapshots": snapshots,
        "lead_only": lead_records,
        "validation_dir": str(
            out_dir.relative_to(root)),
        "validation_assertions": ledger["assertions"],
        "secret_scan": "snapshots/secret_scan.json",
    })
    print("[freeze_wall_a] 完成，wallA 段已合入 freeze_manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
