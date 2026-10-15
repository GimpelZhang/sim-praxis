#!/usr/bin/env bash
# Task7 探针（容器内执行）：MuJoCo 版本核验 + 无渲染步进 + EGL 出片 + Phase0 GPU 探针。
# 用法（容器内）：run_mujoco_probe.sh <probe_out_dir>
set -euo pipefail

OUT_DIR="$1"
PY=/mnt/venvs/simpraxis/bin/python

echo "== versions"
$PY -c "import mujoco, numpy, sys; print('mujoco', mujoco.__version__, 'numpy', numpy.__version__, 'py', sys.version.split()[0])"
$PY -m pip check

echo "== physics step phase (native scene, no GL)"
$PY /workspace/sandbox/probes/probe_mujoco.py --phase step --out "$OUT_DIR/mujoco_step.json"

echo "== egl render phase"
MUJOCO_GL=egl $PY /workspace/sandbox/probes/probe_mujoco.py --phase egl --out "$OUT_DIR/mujoco_egl.json"

echo "== Phase0 real-GPU EGL probe (exit 0 = GPU pass)"
cd /workspace
SIMPRAXIS_RUN_DIR=/mnt/artifacts/simpraxis/runs \
  $PY scripts/verification/verify_egl_real.py

echo T7_MUJOCO_PROBE_DONE
