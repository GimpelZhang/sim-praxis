#!/usr/bin/env bash
# Task8 探针（容器内执行）：从预取 wheel 闭包离线安装 genesis-world，
# 既有 pin 全部作为硬约束（numpy 1.26.4 不松；pydantic 默认钉 2.11.10——
# 仅当用户书面批准 1.4.4 升级路径时以 ALLOW_PYDANTIC_UPGRADE=1 放开该行），
# 随后跑 headless GPU 建场/接触/渲染探针。
# 用法（容器内）：
#   run_genesis_probe.sh <probe_out_dir> <genesis_wheel> <wheels_dir>
# 环境：
#   ALLOW_PYDANTIC_UPGRADE=1  不在约束里钉 pydantic（仅 D 决策书面批准后）
set -euo pipefail

OUT_DIR="$1"
GENESIS_WHL="$2"
WHEELS_DIR="$3"
PY=/mnt/venvs/simpraxis/bin/python
mkdir -p "$OUT_DIR"

cat > /tmp/t8_constraints.txt <<'EOF'
numpy==1.26.4
torch==2.4.1
imageio==2.36.1
mujoco==3.15.0
pydantic==2.11.10
EOF
if [ "${ALLOW_PYDANTIC_UPGRADE:-0}" = "1" ]; then
    grep -v '^pydantic==' /tmp/t8_constraints.txt > /tmp/t8_constraints.tmp
    mv /tmp/t8_constraints.tmp /tmp/t8_constraints.txt
    echo "[probe] ALLOW_PYDANTIC_UPGRADE=1：pydantic 约束已移除（需用户书面批准）"
fi
echo "[probe] constraints:"; cat /tmp/t8_constraints.txt

echo "== 离线安装 genesis-world（--no-index，闭包必须自足）"
test -f "$GENESIS_WHL"
test -d "$WHEELS_DIR"
# 锁内适配（Task8 容器实测 2026-10-15）：闭包内 pygltflib==1.16.0 只有 sdist，
# 离线 --no-index 下 build isolation 会向 find-links 求 setuptools 而失败；
# venv 自带 setuptools 84/wheel 0.48（容器同路径挂载），--no-build-isolation
# 直接复用，不引入任何新包（dry-run 同口径 EXIT=0）。
$PY -m pip install --no-index --no-build-isolation --find-links "$WHEELS_DIR" \
    -c /tmp/t8_constraints.txt "$GENESIS_WHL" 2>&1 | tail -12

echo "== versions + pip check（既有 pin 不得被移动）"
$PY - <<'PYEOF'
import importlib.metadata as m
for n in ["numpy", "torch", "imageio", "mujoco", "genesis-world",
          "numba", "pydantic", "vtk", "quadrants"]:
    try:
        print(f"{n:15s} {m.version(n)}")
    except m.PackageNotFoundError:
        print(f"{n:15s} MISSING")
import numpy
assert numpy.__version__ == "1.26.4", numpy.__version__
PYEOF
# pip check 白名单（决策 D2-T8-CUDA-USERSPACE-LIBS，用户书面裁定 B′，2026-10-15）：
# torch 2.4.1 METADATA 精确钉 nvidia-(cuda-nvrtc|nvjitlink)-cu12==12.4.99 与
# gs-madrona 0.0.10 要求 >=12.8 在任一方向都无法让 pip check 归零；终态保留
# 12.9.86，仅允许这两条已知 torch 冲突，出现任何其他冲突即失败。
# 证据：phase1/task8_track2/install_compat/{DECISION_D2_cuda_libs.json,
# pip_check_under_129.log,RUNTIME_CONFLICT_EVIDENCE.md}
$PY - <<'PYEOF'
import subprocess, sys
p = subprocess.run([sys.executable, "-m", "pip", "check"],
                   text=True, capture_output=True)
lines = [l for l in p.stdout.splitlines() if l.strip()]
expected = (
    "torch 2.4.1", "nvidia-cuda-nvrtc-cu12==12.4.99",
    "nvidia-cuda-nvrtc-cu12 12.9.86",
    "torch 2.4.1", "nvidia-nvjitlink-cu12==12.4.99",
    "nvidia-nvjitlink-cu12 12.9.86",
)
assert len(lines) == 2, f"pip check 冲突行数不为 2：{lines}"
for l, (a, b, c) in zip(sorted(lines),
                        [(expected[0], expected[1], expected[2]),
                         (expected[3], expected[4], expected[5])]):
    assert a in l and b in l and c in l, f"非白名单冲突：{l!r}"
print("pip check whitelist OK：仅 2 条已知 torch 精确钉版冲突（D2 B′ 记录在案）")
PYEOF

echo "== headless Genesis GPU probe"
export HOME=/mnt/caches/home
mkdir -p "$HOME"
export HF_HOME=/mnt/caches/hf
export HF_ENDPOINT=https://hf-mirror.com
$PY /workspace/sandbox/probes/probe_genesis.py \
    --out "$OUT_DIR/genesis_probe.json"

echo "== Phase0 real-GPU EGL probe（exit 0=GPU pass）"
cd /workspace
SIMPRAXIS_RUN_DIR=/mnt/artifacts/simpraxis/runs \
  $PY scripts/verification/verify_egl_real.py

echo T8_GENESIS_PROBE_DONE
