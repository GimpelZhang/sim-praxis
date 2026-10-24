#!/usr/bin/env bash
# Phase 2 Task8 Stage0 流体能力探针（容器内执行）：从预取 wheel 闭包离线
# 安装 genesis-world，既有 pin 全部作为硬约束（numpy==1.26.4 / torch==2.4.1
# 不松），随后跑 headless 真 GPU 粒子流体能力探针（SPH/PBD/MPM 反射式枚举，
# 详见 probe_genesis_fluid.py 模块 docstring）。零方舟付费：本脚本不触网呼。
# 用法（容器内）：
#   run_genesis_fluid_probe.sh <probe_out_dir> <genesis_wheel> <wheels_dir>
# 环境：
#   ALLOW_PYDANTIC_UPGRADE=1  不在约束里钉 pydantic（仅 D 决策书面批准后）
set -euo pipefail

OUT_DIR="$1"
GENESIS_WHL="$2"
WHEELS_DIR="$3"
PY=/mnt/venvs/simpraxis/bin/python
mkdir -p "$OUT_DIR"

cat > /tmp/t8f_constraints.txt <<'EOF'
numpy==1.26.4
torch==2.4.1
imageio==2.36.1
mujoco==3.15.0
pydantic==2.11.10
EOF
if [ "${ALLOW_PYDANTIC_UPGRADE:-0}" = "1" ]; then
    grep -v '^pydantic==' /tmp/t8f_constraints.txt > /tmp/t8f_constraints.tmp
    mv /tmp/t8f_constraints.tmp /tmp/t8f_constraints.txt
    echo "[fluid-probe] ALLOW_PYDANTIC_UPGRADE=1：pydantic 约束已移除（需用户书面批准）"
fi
echo "[fluid-probe] constraints:"; cat /tmp/t8f_constraints.txt

echo "== 离线安装 genesis-world（--no-index，闭包必须自足）"
test -f "$GENESIS_WHL"
test -d "$WHEELS_DIR"
# 锁内适配（Task8 容器实测 2026-10-15，run_genesis_probe.sh 同口径）：闭包内
# pygltflib==1.16.0 只有 sdist，离线 --no-index 下 build isolation 会向
# find-links 求 setuptools 而失败；venv 自带 setuptools/wheel，
# --no-build-isolation 直接复用，不引入任何新包。
$PY -m pip install --no-index --no-build-isolation --find-links "$WHEELS_DIR" \
    -c /tmp/t8f_constraints.txt "$GENESIS_WHL" 2>&1 | tail -12

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
# 与 run_genesis_probe.sh 逐字同口径——仅允许 2 条已知 torch 12.4 精确钉版与
# gs-madrona 要求 >=12.8 的冲突，出现任何其他冲突即失败。
$PY - <<'PYEOF'
import subprocess, sys
p = subprocess.run([sys.executable, "-m", "pip", "check"],
                   text=True, capture_output=True)
lines = [l for l in p.stdout.splitlines() if l.strip()]
assert len(lines) == 2, f"pip check 冲突行数不为 2：{lines}"
expected = (
    ("torch 2.4.1", "nvidia-cuda-nvrtc-cu12==12.4.99",
     "nvidia-cuda-nvrtc-cu12 12.9.86"),
    ("torch 2.4.1", "nvidia-nvjitlink-cu12==12.4.99",
     "nvidia-nvjitlink-cu12 12.9.86"),
)
for l, (a, b, c) in zip(sorted(lines), expected):
    assert a in l and b in l and c in l, f"非白名单冲突：{l!r}"
print("pip check whitelist OK：仅 2 条已知 torch 精确钉版冲突（D2 B′ 记录在案）")
PYEOF

echo "== headless Genesis 流体能力探针（反射枚举/真 GPU/VRAM 看门狗）"
export HOME=/mnt/caches/home
mkdir -p "$HOME"
# Stage0 不下载任何模型/资产：离线双开关兜底（探针只用 Box/Plane 原语）。
export HF_HOME=/mnt/caches/hf
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
$PY /workspace/sandbox/probes/probe_genesis_fluid.py \
    --out "$OUT_DIR/stage0_fluid_report.json"

echo T8_GENESIS_FLUID_PROBE_DONE
