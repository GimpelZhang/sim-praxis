#!/usr/bin/env bash
# Xvfb 仅为需要窗口系统的遗留仿真器提供 DISPLAY；GPU 离屏渲染的正道是 EGL（见探针）。
set -e

# Phase 2 Task0（P4）：非 root（agent 1000）分支。
# - root（Phase 1 旧行为，可回退）：沿用无条件清理 X99 锁；
# - 非 root：绝不 rm 他方锁，只清理属主为当前 uid 的残留（--rm 一次性容器通常无
#   锁；/tmp/.X11-unix 由镜像构建期建好并 chmod 1777，非 root 不得 chmod 它）。
if [ "$(id -u)" -eq 0 ]; then
  rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
else
  for _f in /tmp/.X99-lock /tmp/.X11-unix/X99; do
    if [ -e "$_f" ] && [ -O "$_f" ]; then
      rm -f "$_f"
    fi
  done
fi
Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp +extension GLX +render -noreset >/tmp/xvfb.log 2>&1 &
sleep 1
export DISPLAY=:99
exec "$@"
