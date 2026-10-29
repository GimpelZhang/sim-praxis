#!/usr/bin/env bash
# Phase 2.5 Task0 AC-T0-3：smolagents==1.26.0 依赖探针（0 付费；唯一允许的网络
# 是清华 PyPI 镜像，绝不触方舟；openai-agents 只 dry-run 永不安装）。
#
# 流程：主 venv freeze/pip-check 快照 → 三次 dry-run（smolagents --no-deps /
# smolagents 全依赖 / openai-agents）→ 五 pin 漂移裁决 →
#   零漂移：主 /mnt/venvs/simpraxis 零 extra（--no-deps）真装；
#   有漂移：独立 /mnt/venvs/simpraxis-agentic（py3.10）真装；
# 两条分支都失败才非零退出（STOP，交人工）。
# 裁决件：$SIMPRAXIS_PHASE25_ROOT/estimates/t0_dependency_verdict.json（0600）。
# 幂等：目标版本已在选定 venv 时不重复安装，install 段如实记 already_present。
set -euo pipefail

SIMPRAXIS_ROOT="${SIMPRAXIS_ROOT:-/home/vipuser/sim-praxis}"
cd "$SIMPRAXIS_ROOT"
# shellcheck source=/dev/null
source scripts/00_env.sh >/dev/null

MAIN_PY=/mnt/venvs/simpraxis/bin/python
MAIN_PIP="$MAIN_PY -m pip"
AGENTIC_VENV=/mnt/venvs/simpraxis-agentic
AGENTIC_PY="$AGENTIC_VENV/bin/python"
INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
TARGET='smolagents==1.26.0'
OA_TARGET='openai-agents==0.24.0'

PROBE_DIR="$SIMPRAXIS_PHASE25_ROOT/probes/t0_deps"
EST_DIR="$SIMPRAXIS_PHASE25_ROOT/estimates"
VERDICT="$EST_DIR/t0_dependency_verdict.json"
mkdir -p "$PROBE_DIR" "$EST_DIR"
chmod 700 "$PROBE_DIR" "$EST_DIR" 2>/dev/null || true

echo "[t0-dep] 探针目录：$PROBE_DIR"

# ---- 1) 主 venv 安装前快照（pip check 基线 rc 可能非 0：torch/nvidia 既有告警）----
$MAIN_PIP freeze | sort > "$PROBE_DIR/freeze_before.txt"
set +e
$MAIN_PIP check > "$PROBE_DIR/pipcheck_before.txt" 2>&1
rc_check_before=$?
set -e
echo "[t0-dep] pip check before rc=$rc_check_before（既有基线，非本探针引入）"

# ---- 2) 三次 dry-run（只解析不安装；--report 为 pip 官方 JSON 报告）----
# --ignore-installed 只加在 --no-deps 探针上：幂等重跑时目标已装，不加它
# install[] 会为空，六轻包 requires_dist 约束将无元数据可提取；--no-deps 只
# 重列目标自身（其 wheel 元数据含 requires_dist），不解析任何传递依赖。
# 全依赖探针（2/3）【绝不】加 --ignore-installed——那会无视已装版本把整个闭包
# 30 包全列为 would-install，污染五 pin 漂移裁决。
echo "[t0-dep] dry-run 1/3：smolagents --no-deps（--ignore-installed 保元数据）"
$MAIN_PIP install --dry-run --no-deps --ignore-installed \
  --report "$PROBE_DIR/report_smolagents_nodeps.json" \
  -i "$INDEX_URL" "$TARGET" > "$PROBE_DIR/pip_nodeps.stdout" 2>&1

echo "[t0-dep] dry-run 2/3：smolagents 全依赖"
# L4（2026-10-29 评审修单）：全依赖 dry-run 走网络解析，瞬时抖动不杀脚本——
# 有界重试 3 次（openai 段是留档可失败；本报告是五 pin 裁决的必需输入，
# 三次皆败或报告缺失才 STOP）。
full_rc=1
for _sp_attempt in 1 2 3; do
  set +e
  $MAIN_PIP install --dry-run \
    --report "$PROBE_DIR/report_smolagents_full.json" \
    -i "$INDEX_URL" "$TARGET" > "$PROBE_DIR/pip_full.stdout" 2>&1
  full_rc=$?
  set -e
  if [ "$full_rc" -eq 0 ] && [ -s "$PROBE_DIR/report_smolagents_full.json" ]; then
    break
  fi
  echo "[t0-dep] 警告：全依赖 dry-run 第 ${_sp_attempt}/3 次失败 rc=$full_rc" >&2
done
if [ "$full_rc" -ne 0 ] || [ ! -s "$PROBE_DIR/report_smolagents_full.json" ]; then
  echo "[t0-dep STOP] 全依赖 dry-run 三次皆败 rc=$full_rc，见 $PROBE_DIR/pip_full.stdout" >&2
  exit 1
fi

echo "[t0-dep] dry-run 3/3：openai-agents（永不安装，仅留档）"
set +e
$MAIN_PIP install --dry-run \
  --report "$PROBE_DIR/report_openai_agents.json" \
  -i "$INDEX_URL" "$OA_TARGET" > "$PROBE_DIR/pip_openai.stdout" 2>&1
rc_oa=$?
set -e
# openai-agents 解析失败不致命（留档目的），但报告必须存在；真装绝不可能发生。
if [ "$rc_oa" -ne 0 ] || [ ! -s "$PROBE_DIR/report_openai_agents.json" ]; then
  echo "[t0-dep] 警告：openai-agents dry-run rc=$rc_oa（仅留档，不影响主裁决）" >&2
fi

# ---- 3) 五 pin 漂移裁决（纯本地解析，0 联网）----
# L4：heredoc 用引号定界（路径经 argv 传入），禁止 shell 插值进 Python 文本
# （与本文件 present 探针同一正确形态）。
branch="$($MAIN_PY - "$PROBE_DIR/report_smolagents_full.json" <<'PY'
import sys
sys.path.insert(0, "scripts/phase25")
import t0_dependency_verdict as v
print(v.decide(v.load_report(sys.argv[1]))["branch"])
PY
)"
echo "[t0-dep] 裁决分支：$branch（main_venv=五 pin 零漂移；isolated=有漂移）"

install_flags=()
install_cmd=""
PY="$MAIN_PY"
PIP="$MAIN_PIP"

if [ "$branch" = "main_venv" ]; then
  # ---- 4a) 零漂移：主 venv 零 extra 真装（--no-deps 保证除目标外一个包都不动）----
  present="$($MAIN_PY - <<'PY'
from importlib.metadata import PackageNotFoundError, version
try:
    print(version("smolagents"))
except PackageNotFoundError:
    print("")
PY
)"
  if [ "$present" = "1.26.0" ]; then
    echo "[t0-dep] smolagents 1.26.0 已在主 venv，跳过安装（幂等）"
    install_flags=(--install-already-present)
    install_rc=0
  else
    install_cmd="$MAIN_PIP install --no-deps -i $INDEX_URL $TARGET"
    echo "[t0-dep] 主 venv 真装（零 extra）：$install_cmd（当前 ${present:-未安装}）"
    set +e
    $MAIN_PIP install --no-deps -i "$INDEX_URL" "$TARGET" \
      > "$PROBE_DIR/install_smolagents.stdout" 2>&1
    install_rc=$?
    set -e
    if [ "$install_rc" -ne 0 ]; then
      echo "[t0-dep STOP] 主 venv 安装失败 rc=$install_rc，见 $PROBE_DIR/install_smolagents.stdout" >&2
      exit 1
    fi
    install_flags=(--install-performed --install-rc "$install_rc")
  fi
elif [ "$branch" = "isolated" ]; then
  # ---- 4b) 有漂移：独立 py3.10 venv（主 venv 五 pin 一个都不动）----
  PY="$AGENTIC_PY"
  PIP="$AGENTIC_PY -m pip"
  if [ ! -x "$AGENTIC_PY" ]; then
    echo "[t0-dep] 创建独立 venv：$AGENTIC_VENV（python3.10）"
    if ! command -v python3.10 >/dev/null 2>&1; then
      echo "[t0-dep STOP] python3.10 不存在，无法建独立 venv（两案皆败）" >&2
      exit 1
    fi
    python3.10 -m venv "$AGENTIC_VENV"
  fi
  # 独立 venv 的 before 快照必须在真装之前取（新建 venv 为其基础态）。
  $PIP freeze | sort > "$PROBE_DIR/freeze_before.txt"
  set +e
  $PIP check > "$PROBE_DIR/pipcheck_before.txt" 2>&1
  rc_check_before=$?
  set -e
  install_cmd="$PIP install -i $INDEX_URL $TARGET"
  set +e
  $PIP install -i "$INDEX_URL" "$TARGET" > "$PROBE_DIR/install_smolagents_agentic.stdout" 2>&1
  install_rc=$?
  set -e
  if [ "$install_rc" -ne 0 ]; then
    echo "[t0-dep STOP] 独立 venv 安装也失败 rc=$install_rc（两案皆败，人工核查）" >&2
    exit 1
  fi
  install_flags=(--install-performed --install-rc "$install_rc")
else
  echo "[t0-dep STOP] 未知分支：$branch" >&2
  exit 1
fi

# ---- 5) 安装后快照（选定解释器）----
$PIP freeze | sort > "$PROBE_DIR/freeze_after.txt"
set +e
$PIP check > "$PROBE_DIR/pipcheck_after.txt" 2>&1
rc_check_after=$?
set -e
echo "[t0-dep] pip check after rc=$rc_check_after"

# ---- 6) 构造裁决件（纯本地；原子 0600）----
$MAIN_PY scripts/phase25/t0_dependency_verdict.py \
  --nodeps-report "$PROBE_DIR/report_smolagents_nodeps.json" \
  --full-report "$PROBE_DIR/report_smolagents_full.json" \
  --openai-report "$PROBE_DIR/report_openai_agents.json" \
  --freeze-before "$PROBE_DIR/freeze_before.txt" \
  --freeze-after "$PROBE_DIR/freeze_after.txt" \
  --pipcheck-before "$PROBE_DIR/pipcheck_before.txt" \
  --pipcheck-after "$PROBE_DIR/pipcheck_after.txt" \
  --pipcheck-before-rc "$rc_check_before" \
  --pipcheck-after-rc "$rc_check_after" \
  --interpreter "$($PY -c 'import sys; print(sys.executable)')" \
  --probe-dir "$PROBE_DIR" \
  --install-cmd "$install_cmd" \
  "${install_flags[@]}" \
  --out "$VERDICT"
chmod 600 "$VERDICT" 2>/dev/null || true
echo "[t0-dep] 裁决件已写（0600）：$VERDICT"
