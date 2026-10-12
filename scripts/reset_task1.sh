#!/usr/bin/env bash
# Task1 幂等清理：只清理本计划自己的容器/半成品镜像标签/半成品运行目录。
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source scripts/00_env.sh

# 0. /mnt 空间闸门：重跑构建前可用必须 >100GB
avail=$(df -m --output=avail "$SIMPRAXIS_DATA_ROOT" | tail -1)
[ "$avail" -gt 102400 ] || { echo " /mnt 可用 ${avail}MiB < 100GB，先清理再重跑"; exit 1; }

# 1. 停止并删除本计划容器（不碰其它容器）
if docker ps -aq --filter name=sim_praxis_sandbox | grep -q .; then
  docker rm -f sim_praxis_sandbox
fi

# 2. buildkit 中间层/dangling 镜像回收【不自动化】（m-c）：docker image prune /
#    docker builder prune 是守护进程全局操作（until=24h 也会删掉其它仓库 24h 内的
#    dangling 层），无法按本计划作用域过滤。确需回收时只能由人显式执行：
#      docker images -f dangling=true          # 先看清楚会动谁的层
#      docker image prune -f                   # 人确认后手动执行
#    本脚本永不自动 prune。
# 若 phase0 标签镜像存在但是半成品（构建失败不会打 tag，这里仅防御手工重 tag）
docker images -q sim-praxis/sandbox:phase0 | grep -q . \
  && echo "phase0 镜像已存在；确认是失败产物后执行: docker image rm sim-praxis/sandbox:phase0" || true

# 3. 删除本次未完成的 runs/<ts> 目录：没有 probe_egl.json 的时间戳目录视为半成品
# v4.1 m6（2026-10-12 /mnt 误删事故加固）：rm 前路径后缀硬守卫——
# SIMPRAXIS_RUN_DIR 被污染（离线试跑忘注入 /tmp、变量被改写）时绝不 rm。
case "$SIMPRAXIS_RUN_DIR" in
  */artifacts/simpraxis/runs) ;;
  *) echo "[reset_task1 FAIL] SIMPRAXIS_RUN_DIR='$SIMPRAXIS_RUN_DIR' 不匹配 */artifacts/simpraxis/runs，拒绝任何 rm（铁律 10）" >&2; exit 1 ;;
esac
for d in "$SIMPRAXIS_RUN_DIR"/20*/; do
  [ -d "$d" ] || continue
  # 双保险：被删项必须仍在受保护前缀下，且只允许 20 开头的时间戳目录
  case "$d" in
    "$SIMPRAXIS_RUN_DIR"/20*/) ;;
    *) echo "[reset_task1 FAIL] 异常删除目标 '$d'，拒绝" >&2; exit 1 ;;
esac
  if [ ! -f "$d/probe_egl.json" ]; then
    echo "remove incomplete run: $d"; rm -rf -- "$d"
  fi
done
# latest 若悬空（指向的相对目录已不存在）则移除
if [ -L "$SIMPRAXIS_RUN_DIR/latest" ] && [ ! -e "$SIMPRAXIS_RUN_DIR/latest" ]; then
  rm -f "$SIMPRAXIS_RUN_DIR/latest"
fi
echo "[reset_task1 OK]"
