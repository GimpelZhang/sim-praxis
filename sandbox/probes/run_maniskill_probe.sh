#!/usr/bin/env bash
# Task7 探针（容器内执行）：安装 ManiSkill 3.0.1（既有 pin 不松）+ headless GPU 渲染探针。
# 用法（容器内）：run_maniskill_probe.sh <probe_out_dir>
set -euo pipefail

OUT_DIR="$1"
PY=/mnt/venvs/simpraxis/bin/python
TUNA=https://pypi.tuna.tsinghua.edu.cn/simple

# 既有 pin 全部作为约束：numpy 1.26.4 / torch 2.4.1 / imageio 2.36.1 不松
cat > /tmp/t7_maniskill_constraints.txt <<'EOF'
numpy==1.26.4
torch==2.4.1
imageio==2.36.1
EOF

echo "== install mani_skill==3.0.1（tour clone 同源版本，constraints 不松既有 pin）"
$PY -m pip install -i "$TUNA" --trusted-host pypi.tuna.tsinghua.edu.cn \
    -c /tmp/t7_maniskill_constraints.txt mani_skill==3.0.1 2>&1 | tail -8

echo "== versions + pip check"
$PY - <<'PYEOF'
import numpy, torch, imageio
print("numpy", numpy.__version__, "torch", torch.__version__, "imageio", imageio.__version__)
assert numpy.__version__ == "1.26.4", numpy.__version__
print("torch.cuda.is_available =", torch.cuda.is_available())
PYEOF
$PY -m pip check

echo "== headless GPU render phase"
# HOME 持久化到 /mnt：SAPIEN enable_gpu() 把 PhysX GPU 库下载到
# ~/.sapien/physx/<ver>（sapien/physx/__init__.py 无 env 覆盖，只认 HOME），
# 放 /tmp/home 会随容器销毁、每次 run 重下 GitHub；大文件只落 /mnt。
export HOME=/mnt/caches/home
mkdir -p "$HOME"
# SAPIEN 资产经 HF 镜像缓存（同物理缓存，避免 /root 不可达）
export HF_HOME=/mnt/caches/hf
export HF_ENDPOINT=https://hf-mirror.com
$PY /workspace/sandbox/probes/probe_maniskill.py \
    --out "$OUT_DIR/maniskill_render.json" --env-id PushCube-v1 --n-steps 30

echo "== Phase0 real-GPU EGL probe（torch 就位，exit 0=GPU pass）"
cd /workspace
SIMPRAXIS_RUN_DIR=/mnt/artifacts/simpraxis/runs \
  $PY scripts/verification/verify_egl_real.py

echo T7_MANISKILL_PROBE_DONE
