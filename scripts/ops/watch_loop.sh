#!/usr/bin/env bash
# Phase 2.5 Task 2（AC-T2-2/AC-T2-8）：双签名监控器薄壳。
#
# 用法：
#   scripts/ops/watch_loop.sh --run-dir D [--watchdog-root DIR] \
#       [--poll-s 30] [--silence-s CAP] [--success-grep RE] \
#       [--success-marker PATH] [--container NAME] [--artifact PATH:SHA] \
#       [--notifier feishu|none|fake:PATH] [--ledger PATH]... \
#       [--pause-state PATH]... [--max-ticks N] [--once]
#   scripts/ops/watch_loop.sh guard --run-dir D [--guard-poll-s 30] \
#       [--restart-limit 3]   # 其余 watch-loop 参数同上（AC-T2-8 元监控）
#
# 退出码：0 成功终态；3 死亡签名；4 静默签名；5 轮询预算耗尽/停指；
# 6 元监控重启超限。成功与失败双签名同时检查，双缺+日志静默超阈 = 静默。
#
# 本壳只做仓库根定位与解释器转发；调用前应已 source scripts/00_env.sh。
set -euo pipefail

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_repo="$(cd "$_here/../.." && pwd)"
_py="${SIMPRAXIS_PYTHON:-/mnt/venvs/simpraxis/bin/python}"

_subcmd="watch-loop"
if [ "${1:-}" = "guard" ]; then
    _subcmd="meta-guard"
    shift
fi

# 锁内适配（Phase 2.5）：薄壳不自行 source 00_env.sh——凭据/根由调用 shell
# 注入（00_env 幂等但含凭据形状校验，hermetic 测试环境不应被壳强依赖）。
export PYTHONPATH="$_repo${PYTHONPATH:+:$PYTHONPATH}"
exec "$_py" -m orchestrator.common.watched "$_subcmd" "$@"
