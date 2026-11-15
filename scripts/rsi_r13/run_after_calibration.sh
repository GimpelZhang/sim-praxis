#!/usr/bin/env bash
# R1.3 轮次后续链（校准通过后运行）：probe → practice → study A/B →
# replay A/B（0 POST）→ 正式双审 A/B。
# 任一步非零即停；每步日志落 /tmp/r13chain-<step>.log。
set -uo pipefail
ENVELOPE="${1:?usage: run_after_calibration.sh <envelope> [stamp]}"
STAMP="${2:-$(date -u +%Y%m%dT%H%M%SZ)}"
PY=/mnt/venvs/simpraxis/bin/python
TOK=/mnt/artifacts/simpraxis/rsi_r01/assets/tokenizers/deepseek-ai__DeepSeek-V3.1/tokenizer.json
TOKSHA=32b34a41212e92f62e859cbbea121ae705a1fabbf157d9acf22d134ecd8dcf70
cd /home/vipuser/sim-praxis || exit 1
export SIMPRAXIS_ALLOW_PAID=1
run() {
  local step="$1"; shift
  echo "[chain] $step $(date -u +%FT%TZ)"
  if ! "$@" > "/tmp/r13chain-${step}.log" 2>&1; then
    echo "[chain] STOP at $step (see /tmp/r13chain-${step}.log)"
    return 1
  fi
}
run probe "$PY" -m orchestrator.rsi_r1.cli_r13 probe --envelope "$ENVELOPE" \
  --batch-id "R13-T6-PROBE-${STAMP}" --tokenizer "$TOK" --tokenizer-sha256 "$TOKSHA" || exit 1
run practice "$PY" -m orchestrator.rsi_r1.cli_r13 practice --envelope "$ENVELOPE" \
  --batch-id "R13-T6-PRACTICE-${STAMP}" --tokenizer "$TOK" --tokenizer-sha256 "$TOKSHA" || exit 1
run studyA "$PY" -m orchestrator.rsi_r1.cli_r13 study --envelope "$ENVELOPE" --study A \
  --batch-id "R13-T7-A-${STAMP}" --tokenizer "$TOK" --tokenizer-sha256 "$TOKSHA" || exit 1
run studyB "$PY" -m orchestrator.rsi_r1.cli_r13 study --envelope "$ENVELOPE" --study B \
  --batch-id "R13-T7-B-${STAMP}" --tokenizer "$TOK" --tokenizer-sha256 "$TOKSHA" || exit 1
run replayA "$PY" -m orchestrator.rsi_r1.cli_r13 replay --envelope "$ENVELOPE" --study A || exit 1
run replayB "$PY" -m orchestrator.rsi_r1.cli_r13 replay --envelope "$ENVELOPE" --study B || exit 1
run reviewA "$PY" -m orchestrator.rsi_r1.cli_r13 review --envelope "$ENVELOPE" --study A || exit 1
run reviewB "$PY" -m orchestrator.rsi_r1.cli_r13 review --envelope "$ENVELOPE" --study B || exit 1
echo "[chain] COMPLETE $(date -u +%FT%TZ)"
