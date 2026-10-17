#!/usr/bin/env bash
# SimPraxis 运行时环境解析。用法： source "$SIMPRAXIS_ROOT/scripts/00_env.sh"
# 凭据只来自两处：~/access 按“整行标签精确等值”解析（HF/GitHub/NGC；ARK 可选），
# 与 ~/.bashrc 的 ANTHROPIC_* 族（网关；精确 export 名等值解析，禁止 source）。
# 不回显任何值；禁止 set -x。
# 代理唯一语义：http_proxy/https_proxy 存在且非空=走代理；不存在或空=直连；
#              显式 SIMPRAXIS_USE_PROXY=0 时无条件导出 no_proxy=* 绕过代理。
set +x
umask 077

# ---- 两个可一处改的根变量（m14） ----
SIMPRAXIS_ROOT="${SIMPRAXIS_ROOT:-/home/vipuser/sim-praxis}"
export SIMPRAXIS_ROOT
export KB_ROOT="${KB_ROOT:-/home/vipuser/embodied-ai-sim-knowledge-base}"
SIMPRAXIS_DATA_ROOT="${SIMPRAXIS_DATA_ROOT:-/mnt/artifacts/simpraxis}"
SIMPRAXIS_CACHE_ROOT="${SIMPRAXIS_CACHE_ROOT:-/mnt/caches}"
ACCESS_DIR="${HOME}/access"
export SIMPRAXIS_DATA_ROOT SIMPRAXIS_CACHE_ROOT

# 整行标签精确匹配（trim 后等值；中英文冒号均可），杜绝子串陷阱：
# user_access_methods.txt 中 “NVIDIA NGC API Key username” 是独立标签行，
# “huggingface/github username” 同理；子串匹配会取到用户名。
_sp_value() {
  local file="$1" label="$2"
  [ -f "$file" ] || { echo "[00_env] 缺少凭据文件: $file" >&2; return 1; }
  awk -F'[:：]' -v want="$label" '
    { raw=$0; sub(/\r$/, "", raw)
      key=raw; sub(/[:：].*$/, "", key)
      sub(/^[[:space:]]+/, "", key); sub(/[[:space:]]+$/, "", key)
      if (key==want) {
        idx=index(raw, ":"); if (idx==0) idx=index(raw, "：")
        val=substr(raw, idx+1)
        sub(/^[[:space:]]+/, "", val); sub(/[[:space:]]+$/, "", val)
        printf "%s", val; exit
      }
    }' "$file"
}

# 按凭据类型的形状校验（不只是“长度≥20”，用户名行长度也有 19~21）
# v4.1 主控-2：hint 区分凭据来源——~/access 标签文件缺失与 ~/.bashrc export
# 缺失是两类故障，报错文案不得互相指错位置。
_sp_check_shape() {
  local name="$1" val="$2" ere="$3" hint="${4:-检查 $ACCESS_DIR 标签文件的整行标签是否变化}"
  if [ -z "$val" ]; then
    echo "[00_env] $name 解析为空：必须整行精确匹配；$hint" >&2
    return 1
  fi
  if ! printf '%s' "$val" | grep -Eq "$ere"; then
    echo "[00_env] $name 形状不符（疑似取到 username 行/标签变更或变量值异常，已拒绝；$hint）" >&2
    return 1
  fi
}

HF_TOKEN="$(_sp_value "$ACCESS_DIR/user_access_methods.txt" 'huggingface token')"
GITHUB_TOKEN="$(_sp_value "$ACCESS_DIR/user_access_methods.txt" 'github access token')"
NGC_API_KEY="$(_sp_value "$ACCESS_DIR/user_access_methods.txt" 'NVIDIA NGC API Key')"

_sp_check_shape HF_TOKEN     "$HF_TOKEN"     '^hf_[A-Za-z0-9]{30,}$' \
  '检查 ~/access/user_access_methods.txt 的 huggingface token 标签' || return 1
_sp_check_shape GITHUB_TOKEN "$GITHUB_TOKEN" '^gh[pousr]_[A-Za-z0-9]{30,}$' \
  '检查 ~/access/user_access_methods.txt 的 github access token 标签' || return 1
_sp_check_shape NGC_API_KEY  "$NGC_API_KEY"  '^nvapi-[A-Za-z0-9_-]{20,}$' \
  '检查 ~/access/user_access_methods.txt 的 NVIDIA NGC API Key 标签' || return 1
export HF_TOKEN GITHUB_TOKEN NGC_API_KEY

# ARK_API_KEY（image_editor_model.txt）v4 起为【可选】：仅未来图像编辑功能使用，
# Task 2 网关不再依赖它（网关凭据走 ~/.bashrc 的 ANTHROPIC_* 族，见下）。
# 文件/标签缺失只告警；若解析到值则形状正则仍然硬校验，防止误取。
ARK_API_KEY="$(_sp_value "$ACCESS_DIR/image_editor_model.txt" 'ARK_API_KEY' 2>/dev/null || true)"
if [ -n "$ARK_API_KEY" ]; then
  _sp_check_shape ARK_API_KEY "$ARK_API_KEY" '^ark-[A-Za-z0-9-]{16,}$' \
    '检查 ~/access/image_editor_model.txt 的 ARK_API_KEY 标签' || return 1
  export ARK_API_KEY
else
  echo "[00_env] 提示：未解析到可选 ARK_API_KEY（仅未来图像编辑功能需要，Task 2 不依赖）" >&2
fi

# ---- v4：网关凭据 = ~/.bashrc 的 ANTHROPIC_* 族（方舟 Anthropic Messages /api/coding）----
# 铁律：只按“整行 export NAME=... 或 NAME=...”精确等值解析，禁止 source ~/.bashrc
#       （交互副作用会污染执行环境），禁止回显值；行内 # 之后视为注释。
_sp_bashrc_value() {
  local name="$1" file="${2:-$HOME/.bashrc}"
  [ -f "$file" ] || { echo "[00_env] 缺少: $file" >&2; return 1; }
  awk -v want="$name" '
    { s=$0; sub(/#.*/, "", s) }
    match(s, /^[[:space:]]*(export[[:space:]]+)?/) {
      rest = substr(s, RSTART+RLENGTH)
      if (rest ~ ("^" want "=")) {
        val = rest; sub("^" want "=", "", val)
        sub(/^[[:space:]]*/, "", val); sub(/[[:space:]]*$/, "", val)
        if (val ~ /^".*"$/)       val = substr(val, 2, length(val)-2)
        else if (val ~ ("^\047.*\047$")) val = substr(val, 2, length(val)-2)
        printf "%s", val; exit
      }
    }' "$file"
}

# 优先序：SIMPRAXIS_LLM_* 已导出 -> 继承环境中的 ANTHROPIC_* -> 解析 ~/.bashrc
if [ -n "${SIMPRAXIS_LLM_BASE_URL:-}" ]; then :
elif [ -n "${ANTHROPIC_BASE_URL:-}" ]; then SIMPRAXIS_LLM_BASE_URL="$ANTHROPIC_BASE_URL"
else SIMPRAXIS_LLM_BASE_URL="$(_sp_bashrc_value ANTHROPIC_BASE_URL)" || return 1; fi
if [ -n "${SIMPRAXIS_LLM_TOKEN:-}" ]; then :
elif [ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]; then SIMPRAXIS_LLM_TOKEN="$ANTHROPIC_AUTH_TOKEN"
else SIMPRAXIS_LLM_TOKEN="$(_sp_bashrc_value ANTHROPIC_AUTH_TOKEN)" || return 1; fi
if [ -n "${SIMPRAXIS_LLM_DEFAULT_MODEL:-}" ]; then :
elif [ -n "${ANTHROPIC_MODEL:-}" ]; then SIMPRAXIS_LLM_DEFAULT_MODEL="$ANTHROPIC_MODEL"
else SIMPRAXIS_LLM_DEFAULT_MODEL="$(_sp_bashrc_value ANTHROPIC_MODEL)" || return 1; fi
export SIMPRAXIS_LLM_BASE_URL SIMPRAXIS_LLM_TOKEN SIMPRAXIS_LLM_DEFAULT_MODEL

# 形状硬校验：URL 必须 https 且 host=ark.cn-beijing.volces.com（值不打印）；
# token 为 ark- 前缀（实测 46 字符，下限按形状不按精确长度，供应侧轮换不致误杀）；
# 默认模型名非空且只含安全字符。
printf '%s' "$SIMPRAXIS_LLM_BASE_URL" \
  | grep -Eq '^https://ark\.cn-beijing\.volces\.com(/.*)?$' \
  || { echo "[00_env] ANTHROPIC_BASE_URL 形状/host 不符（拒绝）" >&2; return 1; }
_sp_check_shape SIMPRAXIS_LLM_TOKEN "$SIMPRAXIS_LLM_TOKEN" \
  '^ark-[A-Za-z0-9][A-Za-z0-9._-]{39,}$' \
  '检查 ~/.bashrc 的 ANTHROPIC_AUTH_TOKEN export（或已导出的 SIMPRAXIS_LLM_TOKEN）' || return 1
printf '%s' "$SIMPRAXIS_LLM_DEFAULT_MODEL" \
  | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._-]{2,}$' \
  || { echo "[00_env] ANTHROPIC_MODEL 形状不符（拒绝）" >&2; return 1; }

# HF / 镜像与缓存（全部 /mnt）
export HF_HOME="$SIMPRAXIS_CACHE_ROOT/hf"
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
export FASTEMBED_CACHE_PATH="$SIMPRAXIS_CACHE_ROOT/fastembed"
export MODELSCOPE_CACHE="$SIMPRAXIS_CACHE_ROOT/modelscope"

# Task0b 三层分离：解题方案内容层根（KB 公开仓 solutions/；容器内由 compose
# 覆盖为只读挂载点 /kb），解析器 orchestrator/solutions/resolver.py 读它。
export SIMPRAXIS_SOLUTIONS_ROOT="${SIMPRAXIS_SOLUTIONS_ROOT:-$KB_ROOT/solutions}"

# SimPraxis 数据层：Task0b 起 lancedb 闭源实体迁到 KB data/（/mnt 物理盘，
# $KB_ROOT/data 软链 → /mnt/artifacts/embodied-kb/data），不再在 simpraxis 数据根。
export SIMPRAXIS_LANCEDB_URI="${SIMPRAXIS_LANCEDB_URI:-$KB_ROOT/data/lancedb}"
export SIMPRAXIS_RUN_DIR="${SIMPRAXIS_RUN_DIR:-$SIMPRAXIS_DATA_ROOT/runs}"
export SIMPRAXIS_GATEWAY_STATE="${SIMPRAXIS_GATEWAY_STATE:-$SIMPRAXIS_DATA_ROOT/gateway_state}"
export SIMPRAXIS_TOURS_DIR="${SIMPRAXIS_TOURS_DIR:-/mnt/assets/simpraxis/tours}"
# 模型清点固定路径（v4：inventory 仅供换供请示取证，router 不读它做闸；v4.2 决策 3）
export SIMPRAXIS_INVENTORY_LATEST="${SIMPRAXIS_INVENTORY_LATEST:-$SIMPRAXIS_RUN_DIR/inventory/latest/ark_models.json}"

# Task0b：$SIMPRAXIS_LANCEDB_URI 指向 KB data/ 软链后的闭源数据层，其建层/建链
# 只由 scripts/kb/migrate_data_layer.py 负责；env 绝不预建该路径，否则缺链的新克隆
# 会被静默写穿成 KB 仓实体目录（split-brain），且绕过 check_solutions 的实体守卫。
mkdir -p "$HF_HOME" "$FASTEMBED_CACHE_PATH" "$MODELSCOPE_CACHE" \
         "$SIMPRAXIS_RUN_DIR/inventory" \
         "$SIMPRAXIS_RUN_DIR/model_validate" \
         "$SIMPRAXIS_GATEWAY_STATE" "$SIMPRAXIS_TOURS_DIR"

# 代理：SIMPRAXIS_USE_PROXY=0 无条件绕过；其余情形继承 shell（空=直连）
if [ "${SIMPRAXIS_USE_PROXY:-1}" = "0" ]; then
  export no_proxy='*' NO_PROXY='*'
fi

echo "[00_env] OK：~/access 三凭据 + ~/.bashrc ANTHROPIC_* 网关三件按形状注入（值已隐藏）；endpoint host=$(printf '%s' "$SIMPRAXIS_LLM_BASE_URL" | sed -E 's#^(https://[^/]+).*#\1#')；HF_ENDPOINT=$HF_ENDPOINT；数据根=$SIMPRAXIS_DATA_ROOT"

# ---- Phase 1：飞书自建应用凭据（Task1 起需要；可选，缺失只告警，不阻断 Phase 0 命令）----
# 值只从 ~/access/feishu_access.txt 按整行标签等值解析（复用 _sp_value，无正则提取），不回显。
# 三件套标签与三条形状正则已于 2026-10-13 对真实凭据文件实读核验通过（Phase 1 详细计划 v1.2 §2 Task0）。
FEISHU_ACCESS="$ACCESS_DIR/feishu_access.txt"
FEISHU_APP_ID="$(_sp_value "$FEISHU_ACCESS" 'feishu app id' 2>/dev/null || true)"
FEISHU_APP_SECRET="$(_sp_value "$FEISHU_ACCESS" 'feishu app secret' 2>/dev/null || true)"
FEISHU_RECEIVE_ID="$(_sp_value "$FEISHU_ACCESS" 'feishu receive id' 2>/dev/null || true)"
if [ -n "$FEISHU_APP_ID" ] && [ -n "$FEISHU_APP_SECRET" ] && [ -n "$FEISHU_RECEIVE_ID" ]; then
  _sp_check_shape FEISHU_APP_ID "$FEISHU_APP_ID" '^cli_[A-Za-z0-9]{10,}$' \
    '检查 ~/access/feishu_access.txt 的 feishu app id 标签' || return 1
  _sp_check_shape FEISHU_APP_SECRET "$FEISHU_APP_SECRET" '^[A-Za-z0-9_-]{20,}$' \
    '检查 ~/access/feishu_access.txt 的 feishu app secret 标签' || return 1
  _sp_check_shape FEISHU_RECEIVE_ID "$FEISHU_RECEIVE_ID" '^ou_[A-Za-z0-9]{30,}$' \
    '检查 ~/access/feishu_access.txt 的 feishu receive id 标签' || return 1
  export FEISHU_APP_ID FEISHU_APP_SECRET FEISHU_RECEIVE_ID
else
  echo "[00_env] 提示：飞书三件套未解析全（Task1 前需 feishu app id/secret/receive id 三个整行标签）" >&2
fi

# Phase 1 证据/状态根（全部 /mnt；state/quota 700；LangGraph checkpoint 与 pending gates 均在此）
export SIMPRAXIS_PHASE1_ROOT="${SIMPRAXIS_PHASE1_ROOT:-$SIMPRAXIS_DATA_ROOT/phase1}"
mkdir -p "$SIMPRAXIS_PHASE1_ROOT"/{task0_env,task1_feishu,task2_direction,task3_graph,task4_runner,task5_judge,task6_learnings,task9_milestone,state,quota} \
         "$SIMPRAXIS_RUN_DIR/phase1"
chmod 700 "$SIMPRAXIS_PHASE1_ROOT/state" "$SIMPRAXIS_PHASE1_ROOT/quota" 2>/dev/null || true

# ---- Phase 2 Task0：独立新账根（P2/A12）、非 root 容器账号（P4）、v2 签收件、G-4 可写探针 ----
# Phase 2 台账独立新账：与 Phase 1 封存账同盘不同根，seq 从 0 起，Phase 1 账永不被写打开。
export SIMPRAXIS_PHASE2_ROOT="${SIMPRAXIS_PHASE2_ROOT:-$SIMPRAXIS_DATA_ROOT/phase2}"
# 非 root 容器运行身份（compose user: 插值；与宿主 vipuser 1000:1000 对齐）。
export SIMPRAXIS_UID="${SIMPRAXIS_UID:-1000}"
export SIMPRAXIS_GID="${SIMPRAXIS_GID:-1000}"
mkdir -p "$SIMPRAXIS_PHASE2_ROOT"/quota/approvals "$SIMPRAXIS_PHASE2_ROOT"/runs \
         "$SIMPRAXIS_PHASE2_ROOT"/state "$SIMPRAXIS_RUN_DIR/phase2"
chmod 700 "$SIMPRAXIS_PHASE2_ROOT/quota" "$SIMPRAXIS_PHASE2_ROOT/state" 2>/dev/null || true

# quota_pause v2 schema 签收操作件：仓内模板 0600 安装到新账 quota/，只装一次，
# 已存在（尤其已签收件）绝不覆盖；值不入 git。
_sp_pause_signed="$SIMPRAXIS_PHASE2_ROOT/quota/quota_pause_schema_v2.signed.json"
if [ ! -e "$_sp_pause_signed" ] && [ -f "$SIMPRAXIS_ROOT/scripts/milestone2/quota_pause_schema_v2.template.json" ]; then
  cp "$SIMPRAXIS_ROOT/scripts/milestone2/quota_pause_schema_v2.template.json" "$_sp_pause_signed"
  chmod 600 "$_sp_pause_signed"
fi

# arch G-4（Task0）：非 root 改造后的可写探针——以当前宿主身份（= 容器 1000:1000
# 的同一 uid）对非 root 容器的 bind 源做写/删验证；任一项不可写则 00_env 失败，
# 避免容器跑起来才 EACCES。只碰 simpraxis 自有 /mnt 字面路径。
_sp_writable_probe() {
  local dir="$1" probe
  probe="$dir/.write_probe.$$"
  if ( umask 077 && : > "$probe" ) 2>/dev/null; then
    rm -f "$probe"
  else
    echo "[00_env] G-4 可写探针失败：$dir 对 uid=$(id -u) 不可写（非 root 容器将 EACCES）" >&2
    return 1
  fi
}
_sp_writable_probe "$SIMPRAXIS_RUN_DIR" || return 1
_sp_writable_probe "$HF_HOME" || return 1
_sp_writable_probe "$SIMPRAXIS_PHASE2_ROOT/quota" || return 1
echo "[00_env] Phase2：新账根=$SIMPRAXIS_PHASE2_ROOT（quota/state 700）；容器身份=${SIMPRAXIS_UID}:${SIMPRAXIS_GID}；G-4 可写探针通过；v2 签收件=$([ -e "$_sp_pause_signed" ] && echo present || echo missing)"
