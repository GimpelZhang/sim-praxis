#!/usr/bin/env python3
"""Phase 2 Task6：V1 冻结回放的同法 VRAM 峰值预置（0 付费、宿主直跑工具）。

Task6 evaluate 接缝要求两侧 VRAM sidecar：V2=batch workdir/vram_peak.json
（task6 工厂以 gpu_sampler wrapper 同包裹采样），V1=
``$PHASE2_ROOT/evolution_sources/<thread>/v1_vram_peak.json``（本脚本预置）。
冻结 V1 本体无仪器化且**禁止改冻结脚本**，故以**同一个**
orchestrator/common/gpu_sampler.py 设备级 wrapper 包裹冻结 V1 的完整 9 跑
batch，两侧同 wrapper/同机器/同设备 0 口径，Pareto 显存列才可比。

本脚本是**引擎侧工具，不是解题脚本**（三层分离：解题脚本只在内容层
$SIMPRAXIS_SOLUTIONS_ROOT，经 resolver 逐件 sha256/C4 校验后 stage）：
  1. resolve_solution(genesis 方案 id).stage(workdir) 取冻结 V1 入口；
  2. 复用 task8 的选窗标定件 stage 与 versions.txt 口径、task6 工厂 genesis
     场景的同一 env_extra（HOME/HF/GS_ENABLE_ZEROCOPY/PYOPENGL_PLATFORM…）；
  3. gpu_sampler 包裹 V1 batch（宿主 /mnt/venvs python + EGL，非 docker），
     stdout/stderr 实时透传，超时 2400s（SIGTERM 经 wrapper killpg 整组转发，
     30s 宽限后只对 wrapper 直接 SIGKILL——batch 孙进程靠 wrapper preexec 的
     PR_SET_PDEATHSIG 由内核兜底 SIGKILL，孤儿链闭合，硬规则 5）；
  4. rc!=0 保留全部产物、不安装 sidecar；rc=0 校验 sidecar 契约后以 0600
     原子拷到 evolution_sources（同内容幂等、内容不同需 --force）。

零付费可静态证明：本脚本不构造网关、不触方舟/飞书，全部计算离线；实付只
可能来自后续图运行期（V1 回放本身是纯仿真）。

用法：
  python scripts/phase2/provision_v1_vram.py [--thread t6-...] [--force]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from orchestrator.common.fsutil import (  # noqa: E402
    atomic_write_bytes,
    atomic_write_text,
)
from orchestrator.common.runtime_paths import phase2_runs_root  # noqa: E402
from orchestrator.graph import task6  # noqa: E402
from orchestrator.graph import task8  # noqa: E402
from orchestrator.graph.task8 import (  # noqa: E402
    BATCH_TIMEOUT_S,
    CALIBRATION_NAME,
    ENGINE_SCENARIOS,
    SEEDS,
    VENV_PY,
)
from orchestrator.solutions import SolutionError, resolve_solution  # noqa: E402

DEFAULT_THREAD = "t6-genesis-ascent-20261016a"
REPLAY_DIR_PREFIX = "v1_vram_replay_"
GPU_SAMPLER = _REPO_ROOT / "orchestrator" / "common" / "gpu_sampler.py"
SIGTERM_GRACE_S = 30


# ---------------------------------------------------------------------------
# 纯函数（路径/命令构造、幂等与 --force 判定；单测不跑真实回放）
# ---------------------------------------------------------------------------

def replay_workdir(thread_id: str) -> Path:
    """V1 回放 workdir：phase2 runs/task6_ascent/v1_vram_replay_<thread>。"""
    return (phase2_runs_root() / "task6_ascent"
            / f"{REPLAY_DIR_PREFIX}{thread_id}")


def target_sidecar(thread_id: str) -> Path:
    """V1 sidecar 安装目标：task6 evolution_sources/<thread>/v1_vram_peak.json。"""
    return task6.sources_dir(thread_id) / f"v1_{task6.VRAM_NAME}"


def build_wrapper_cmd(workdir: Path, entrypoint: Path, cal: Path) -> list[str]:
    """与 task6 工厂逐字同形态的 wrapper 前缀（sidecar 先落 workdir）。"""
    return [
        VENV_PY, str(GPU_SAMPLER),
        "--sidecar", str(workdir / task6.VRAM_NAME), "--",
        VENV_PY, str(entrypoint), "batch",
        "--out-dir", str(workdir),
        "--seeds", str(SEEDS),
        "--calibration-json", str(cal),
    ]


def genesis_env_extra() -> dict[str, str]:
    """env_extra 必须与 task6 工厂 genesis 场景同源（HOME/HF/EGL/zerocopy）。"""
    env_extra = {
        "HOME": "/mnt/caches/home",
        "HF_HOME": "/mnt/caches/hf",
        "HF_ENDPOINT": "https://hf-mirror.com",
        "SIMPRAXIS_USE_PROXY": "0",
    }
    env_extra.update(ENGINE_SCENARIOS["genesis"]["env"])
    return env_extra


def _guarded_replay_path(workdir: Path) -> None:
    """删除守卫（硬规则 6）：只许删 task6_ascent/v1_vram_replay_* 自有字面目录。"""
    resolved = workdir.resolve()
    base = (phase2_runs_root() / "task6_ascent").resolve()
    if resolved.parent != base \
            or not resolved.name.startswith(REPLAY_DIR_PREFIX) \
            or workdir.is_symlink():
        raise RuntimeError(
            f"删除守卫拒绝（非 V1 VRAM 回放自有目录）：{workdir}")


def prepare_workdir(thread_id: str, force: bool) -> Path:
    """已有 sidecar 即拒绝（防覆盖实跑产物）；--force 先 ls 留痕再清本目录。"""
    workdir = replay_workdir(thread_id)
    sidecar = workdir / task6.VRAM_NAME
    if workdir.exists() and sidecar.is_file():
        if not force:
            raise FileExistsError(
                f"{sidecar} 已存在：拒绝覆盖已有回放产物；确认重建请加 "
                "--force（仅清空该回放目录后重跑，evolution_sources 另判）")
        _guarded_replay_path(workdir)
        # 删除守卫：rm 前先 ls，目标必须是自有字面路径（rglob 只列不删）。
        for p in sorted(workdir.rglob("*")):
            print(f"[force] 清重建：{p}", file=sys.stderr)
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir


def _require_vram_contract(data: dict, sidecar: Path) -> float:
    """镜像 task6._vram_gb 契约：{"vram_peak_gb": 正有限实数}，否则 fail-closed。"""
    v = data.get(task6.VRAM_KEY)
    if isinstance(v, bool) or not isinstance(v, (int, float)) \
            or not math.isfinite(float(v)) or float(v) <= 0.0:
        raise ValueError(
            f"V1 VRAM sidecar {task6.VRAM_KEY} 必须是正有限实数：{sidecar}={v!r}")
    return float(v)


def install_sidecar(thread_id: str, workdir: Path, force: bool) -> dict:
    """校验 workdir/vram_peak.json 后 0600 原子安装；同字节幂等，异内容需 --force。"""
    src = workdir / task6.VRAM_NAME
    raw = src.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"V1 VRAM sidecar 顶层必须是对象：{src}")
    peak_gb = _require_vram_contract(data, src)

    dst = target_sidecar(thread_id)
    if dst.is_file():
        if dst.read_bytes() == raw:
            action = "idempotent"
        elif not force:
            raise FileExistsError(
                f"{dst} 已存在且内容不同：拒绝覆盖；显式 --force 才重安装")
        else:
            atomic_write_bytes(dst, raw)
            action = "overwritten"
    else:
        atomic_write_bytes(dst, raw)
        action = "written"
    return {
        "thread_id": thread_id,
        "action": action,
        "replay_workdir": str(workdir),
        "replay_sidecar": str(src),
        "installed_sidecar": str(dst),
        task6.VRAM_KEY: peak_gb,
        "vram_baseline_gb": data.get("vram_baseline_gb"),
        "samples": data.get("samples"),
        "device_name": data.get("device_name"),
        "child_rc": data.get("child_rc"),
        "elapsed_s": data.get("elapsed_s"),
    }


# ---------------------------------------------------------------------------
# 实跑（宿主直跑；lead 三签后由主会话触发，单测不进入本函数）
# ---------------------------------------------------------------------------

def run_replay(thread_id: str, force: bool,
               timeout_s: int = BATCH_TIMEOUT_S) -> int:
    scenario = ENGINE_SCENARIOS["genesis"]
    workdir = prepare_workdir(thread_id, force)

    try:
        staged = resolve_solution(scenario["solution_id"]).stage(workdir)
    except SolutionError as exc:
        print(f"V1 冻结方案 stage 失败（{scenario['solution_id']}）：{exc}",
              file=sys.stderr)
        return 10

    versions = workdir / "versions.txt"
    if not versions.exists():
        atomic_write_text(versions, task8._installed_versions())
    cal = workdir / CALIBRATION_NAME
    if not cal.exists():
        task8._stage_calibration(cal)

    cmd = build_wrapper_cmd(workdir, staged.entrypoint, cal)
    env = dict(os.environ)
    env.update(genesis_env_extra())

    print(f"[provision] thread={thread_id} workdir={workdir}", flush=True)
    print(f"[provision] cmd[0:3]={' '.join(cmd[:3])} …（stdout/stderr 实时透传）",
          flush=True)
    # stdout/stderr 不 capture：实时透传，仿真进度直连终端。
    proc = subprocess.Popen(cmd, cwd=str(workdir), env=env)
    try:
        rc = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        # SIGTERM 给 wrapper（本进程的同组直接子进程；独立成组的是 batch
        # 孙进程）：wrapper handler killpg 整组优雅转发，不留孤儿。
        print(f"[provision] 回放超过 {timeout_s}s，SIGTERM 经 wrapper 整组回收",
              file=sys.stderr, flush=True)
        proc.terminate()
        try:
            rc = proc.wait(timeout=SIGTERM_GRACE_S)
        except subprocess.TimeoutExpired:
            # 宽限后仍不死：只对 wrapper 直接发 SIGKILL（wrapper 与本工具同
            # 进程组，killpg 会自杀；wrapper 内 batch 孙进程独立成组，靠其
            # preexec 的 PR_SET_PDEATHSIG 被内核 SIGKILL，孤儿链同样闭合）。
            proc.kill()
            rc = proc.wait()
        rc = rc if rc != 0 else 124

    if rc != 0:
        print(f"[provision] V1 回放失败 rc={rc}；保留全部产物 {workdir}，"
              "不安装 sidecar", file=sys.stderr)
        return rc if rc > 0 else 124

    info = install_sidecar(thread_id, workdir, force)
    print(json.dumps(info, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="provision_v1_vram.py",
        description="V1 冻结回放 VRAM sidecar 预置（gpu_sampler 同法口径）")
    parser.add_argument("--thread", default=DEFAULT_THREAD,
                        help=f"Task6 线程 id（默认 {DEFAULT_THREAD}）")
    parser.add_argument("--force", action="store_true",
                        help="已有回放 sidecar 时清本目录重建；目标异内容时覆盖")
    args = parser.parse_args(argv)
    return run_replay(args.thread, args.force)


if __name__ == "__main__":
    sys.exit(main())
