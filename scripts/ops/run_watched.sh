#!/usr/bin/env bash
# Phase 2.5 Task 2（AC-T2-1）：长跑包装器薄壳。
#
# 用法：
#   scripts/ops/run_watched.sh <name> [--cwd DIR] [--silence-s CAP] \
#       [--success-grep RE] [--success-marker PATH] [--container NAME] \
#       [--artifact PATH:SHA256]... [--watchdog-root DIR] -- <cmd...>
#
# 七件套（pid/start_iso/cmd_sha256/stdout/stderr/heartbeat/exit_code）落
# $SIMPRAXIS_PHASE25_ROOT/watchdog/runs/<name>-<ts>/；心跳 toucher 是独立
# Python 小程序，preexec 上 prctl(PR_SET_PDEATHSIG, SIGKILL)，父死内核级
# 同死（实现见 orchestrator/common/watched.py）。
#
# 本壳只做仓库根定位与解释器转发；调用前应已 source scripts/00_env.sh。
set -euo pipefail

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_repo="$(cd "$_here/../.." && pwd)"
_py="${SIMPRAXIS_PYTHON:-/mnt/venvs/simpraxis/bin/python}"

# 锁内适配（Phase 2.5）：薄壳不自行 source 00_env.sh——凭据/根由调用 shell
# 注入（00_env 幂等但含凭据形状校验，hermetic 测试环境不应被壳强依赖）。
export PYTHONPATH="$_repo${PYTHONPATH:+:$PYTHONPATH}"
exec "$_py" -m orchestrator.common.watched run-watched "$@"
