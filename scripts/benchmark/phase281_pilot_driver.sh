#!/usr/bin/env bash
# Phase 2.8.1 P281-7 Pilot 驱动器（计划 §11.4 步骤 3–5 的薄壳，可 review）。
#
#   scripts/ops/run_watched.sh p281-pilot \
#     --watchdog-root "$ROOT/watchdog" --silence-s 600 \
#     --success-marker "$ROOT/analysis/pilot_report.json" \
#     -- bash scripts/benchmark/phase281_pilot_driver.sh "$ROOT"
#
# 本脚本**不做**任何决策：臂序来自 `pre_registration/trial_order.json`（冻结件），
# 帽长来自批准件（`quota/approvals/P281-B1-{1,2,3}.json`），判决来自
# `orchestrator.benchmark.phase281.analysis`（§11.5 规则）。它只按顺序调用，
# 任何一步非 0 立即停（`set -e`）：**静默续跑**会把「某个 Trial 没跑」读成「跑完了」。
#
# 锁内适配 A7-2（计划没写、但硬前置）：§8.4 / AC-P281-4-18 要求「真实付费 Trial
# 之前必须先把运行形态探针跑出来」，而 §11.4 的步骤 0–5 没有这一步。这里补第 2.5 步
# ——探针 **0 付费**，逐任务跑；任一任务 `rc != 0` 或 `import_ok != true` 即停，
# 绝不拿付费额度去证明一个已知的环境错误。
#
# 锁内适配 A7-3：§11.4 的步骤 0（LOCK --verify / 容器空 / 显存 ≤200 MiB）与步骤 1
# （写批准件）**不在**本脚本里——它们必须在开跑前由人看着做完（批准件是「先写再跑」
# 的审计物，脚本自己写就等于自签自跑）。本脚本只断言它们已经就位。
set -euo pipefail

ROOT="${1:-}"
# 第二个参数：计费批量身份前缀。
#   trial_id  —— 缺省，首轮（2026-11-07 run）的实际口径：逐 Trial 批准件
#                `<trial_id>.json`，`batch_id = trial_id`（偏差 D3，登记在案）。
#   P281-B1b  —— 重跑批：按计划 §11.3 的任务票记，`batch_id = <prefix>-<task_no>`。
# 传前缀时脚本会**先检查该前缀的三张任务票在场**（「先写再跑」），不代写。
BATCH_PREFIX="${2:-trial_id}"
if [ -z "$ROOT" ]; then
  echo "用法: $0 <data-root> [batch-prefix]" >&2
  echo "  例: $0 /mnt/artifacts/simpraxis/phase281 P281-B1b" >&2
  exit 2
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
# shellcheck disable=SC1091
source scripts/00_env.sh

PY=/mnt/venvs/simpraxis/bin/python
REP=0
ORDER="$ROOT/pre_registration/trial_order.json"
TASKS=(mujoco_contact_push_v1 libero_env_adapter_v1 icy_ground_dr_v1)

log() { printf '[p281-pilot] %s\n' "$*" >&2; }

# --- 前置断言（A7-3）：不在脚本里做，只拒绝在没做的情况下开跑 -------------------
if [ "${SIMPRAXIS_ALLOW_PAID:-}" != "1" ]; then
  log "拒绝开跑：真实 Trial 会经网关计费，请显式 SIMPRAXIS_ALLOW_PAID=1"
  exit 3
fi
# 前置断言：任务票必须在场。`trial_id` 前缀时按 §11.3 原案查 `P281-B1-{1,2,3}`
# **注意 D3**：首轮链路的计费批量身份是逐 Trial 的（`trial_harness._ensure_approval`
# 自写 `<trial_id>.json`，`batch_id=trial_id`），这三张票在账本里 `batch_used=0`、
# 不约束任何一次调用——所以这里是「按计划验件」，不是「有它就放行预算」；逐 Trial
# 10 呼帽由 CapGuardGateway 强制。重跑批传前缀（如 P281-B1b）时，票**真的**是闸。
if [ "$BATCH_PREFIX" = "trial_id" ]; then
  TICKET_PREFIX=P281-B1
else
  TICKET_PREFIX="$BATCH_PREFIX"
fi
for task_no in 1 2 3; do
  approval="$ROOT/quota/approvals/$TICKET_PREFIX-$task_no.json"
  if [ ! -f "$approval" ]; then
    log "拒绝开跑：缺批准件 $approval（§11.3「先写再跑」，不给先跑后补留口子）"
    exit 3
  fi
done
if [ ! -f "$ORDER" ]; then
  log "拒绝开跑：缺冻结臂序 $ORDER"
  exit 3
fi

# --- 2.5) 运行形态探针（0 付费；A7-2） ----------------------------------------
mkdir -p "$ROOT/analysis"
for spec in "${TASKS[@]}"; do
  log "探针 $spec"
  if ! "$PY" -I scripts/benchmark/phase281_run_trial.py \
        --task "$spec" --probe-run-form --root "$ROOT"; then
    log "停止：$spec 的运行形态探针红（rc!=0 或 import_ok!=true）——不开付费批次"
    exit 4
  fi
done

# --- 3) 按 trial_order.json 顺序跑 6 个 Trial --------------------------------
for task_no in 1 2 3; do
  spec="${TASKS[$((task_no - 1))]}"
  key="$spec|rep$REP"
  arms="$("$PY" -c 'import json,sys; d=json.load(open(sys.argv[1])); print(" ".join(d[sys.argv[2]]))' \
            "$ORDER" "$key")"
  if [ -z "$arms" ]; then
    log "停止：trial_order.json 没有键 $key"
    exit 5
  fi
  for arm in $arms; do
    log "Trial $spec / $arm / rep$REP"
    if [ "$BATCH_PREFIX" = "trial_id" ]; then
      "$PY" -I scripts/benchmark/phase281_run_trial.py \
        --task "$spec" --arm "$arm" --rep "$REP" --root "$ROOT" --json
    else
      "$PY" -I scripts/benchmark/phase281_run_trial.py \
        --task "$spec" --arm "$arm" --rep "$REP" --root "$ROOT" --json \
        --batch "$TICKET_PREFIX-$task_no"
    fi
  done
done

# --- 4) 逐 Trial 评测（0 付费；本阶段自己的评测器） ---------------------------
mkdir -p "$ROOT/evaluator_results"
for td in "$ROOT"/runs/*/*/rep"$REP"/; do
  [ -d "$td" ] || { log "停止：没有 Trial 目录 $td"; exit 6; }
  task="$(basename "$(dirname "$(dirname "$td")")")"
  arm="$(basename "$(dirname "$td")")"
  rep="$(basename "$td")"
  out="$ROOT/evaluator_results/${task}_${arm}_${rep}.json"
  log "评测 $task / $arm / $rep -> $out"
  "$PY" -I scripts/benchmark/phase281_evaluate.py \
    --trial-dir "$td" \
    --anchor "$ROOT/private/anchor_manifest.json" \
    --out "$out"
done

# --- 5) Pilot 分析 + go/no-go（0 付费；§11.5 规则，可复算） --------------------
# `--verify-register` 让 G281-2（LOCK --verify rc==0）由分析器实跑一次登记进
# pilot_report.five_gates，而不是留 null 让读者去猜（§12.2 把它列为**机判门**）。
log "分析：pilot_report.json"
"$PY" -m orchestrator.benchmark.phase281.analysis --stage pilot \
  --root "$ROOT" --out "$ROOT/analysis/pilot_report.json" \
  --probe-env --verify-register

# --- 6) 收尾：容器清干净、显存回基线（硬规则 5） ------------------------------
# 锁内适配 A7-4：本仓 compose 件在 sandbox/docker-compose.yml，仓库根 `docker compose`
# 只会报 "no configuration file provided" —— 那句提示会被读成「容器干净」，是假绿。
log "收尾：docker compose -f sandbox/docker-compose.yml down"
docker compose -f "$REPO_ROOT/sandbox/docker-compose.yml" down \
  || log "提示：compose down 返回非 0（无运行中服务时属正常）"
nvidia-smi --query-gpu=memory.used --format=csv,noheader
log "完成"
