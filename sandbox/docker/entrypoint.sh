#!/usr/bin/env bash
# Xvfb 仅为需要窗口系统的遗留仿真器提供 DISPLAY；GPU 离屏渲染的正道是 EGL（见探针）。
set -e
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp +extension GLX +render -noreset >/tmp/xvfb.log 2>&1 &
sleep 1
export DISPLAY=:99
exec "$@"
