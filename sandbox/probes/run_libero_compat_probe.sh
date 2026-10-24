#!/usr/bin/env bash
# Task7 B 线 Stage0（容器内执行）：LIBERO/robosuite 与共享 mujoco 3.15.0
# 兼容探针。隔离 venv 建在 /home/agent/runs/phase2/venvs/libero——Task0 非 root
# 化后 D-4 锁内适配路径，/root 树下的 root 时代旧位禁用（非 root 容器不可写）；
# --system-site-packages 复用共享 torch 2.4.1/numpy 1.26.4，只复用不写共享
# site-packages。随后跑 robosuite==1.4.0 + bddl==1.0.1 最小链 + LIBERO editable
# 安装后的 import 链，
# 产物 stage0_compat_report.json（simpraxis.task7.stage0_compat.v1），默认落
# /home/agent/runs/phase2/task7/stage0/（宿主侧经 bind mount 落 /mnt/artifacts）。
#
# D-4 降级口径：探针两种结局都是合法机判产物——
#   compatible                → 可把 Stage1 运行案写进 Gate1 卡；
#   conflict_isolated_failed  → STOP 报用户，降级为「结构化巡礼（A 线已完成）
#                               + 兼容性白皮书」，绝不伪装跑通、绝不升级共享 mujoco。
#
# 硬规则 8：清华源；宿主与共享 venv pin 一律不动（探针自身约束只钉 numpy/torch，
# 刻意不钉 mujoco，让版本漂移在隔离 venv 内如实暴露）。
# 硬规则 10：不执行 tour 任何脚本/notebook（仅 pip install -e 与 import）。
#
# 用法（容器内，以非 root uid=1000 的 agent 身份执行，无特权提升）：
#   run_libero_compat_probe.sh [out_dir] [tour_dir] [venv_path]
# 默认 out_dir=/home/agent/runs/phase2/task7/stage0（reports/logs 同落 runs 区）。
set -euo pipefail

OUT_DIR="${1:-/home/agent/runs/phase2/task7/stage0}"
TOUR_DIR="${2:-/mnt/assets/simpraxis/tours/LIBERO_tour}"
VENV_PATH="${3:-/home/agent/runs/phase2/venvs/libero}"
PY=/mnt/venvs/simpraxis/bin/python

# 目录以当前容器 uid（agent，1000:1000）创建：runs 区由 bind mount 落到宿主
# /mnt/artifacts；不做特权提升、不改属主。
mkdir -p "$OUT_DIR"
# 隔离 venv 根落在容器 runs 区（挂载宿主 /mnt 大文件区），不写宿主共享 venv。
mkdir -p "$(dirname "$VENV_PATH")"

# A 线 tour 必须已结构化挂载（存在性自检；不读其 requirements 做解析）。
test -f "$TOUR_DIR/setup.py" || {
    echo "[probe] tour 目录缺 setup.py：$TOUR_DIR（A 线结构化克隆未挂载？）" >&2
    exit 3
}

echo "== Task7 B-line Stage0: LIBERO 隔离 venv 兼容探针"
echo "    out=$OUT_DIR tour=$TOUR_DIR venv=$VENV_PATH"
# 探针内部：建 venv（--system-site-packages）→ 最小链安装 → editable 安装
# → 定向版本查询 → numpy/torch/mujoco/robosuite/bddl/libero import 链。
$PY /workspace/sandbox/probes/probe_libero_compat.py \
    --out "$OUT_DIR/stage0_compat_report.json" \
    --tour-dir "$TOUR_DIR" \
    --venv "$VENV_PATH"

echo "== 报告：$OUT_DIR/stage0_compat_report.json"
# 与 run_genesis_fluid_probe.sh 兄弟约定一致：裸 echo 完成标记，不设死变量。
echo T7_LIBERO_STAGE0_DONE
