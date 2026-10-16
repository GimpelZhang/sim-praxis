#!/usr/bin/env bash
# 对 git 已跟踪 + 未跟踪但将被加入的文件做密钥模式扫描；命中即非零退出。
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

mapfile -t FILES < <( { git ls-files; git ls-files --others --exclude-standard; } | sort -u )
[ "${#FILES[@]}" -eq 0 ] && { echo "[secret-scan] 无文件"; exit 0; }

PATTERNS=(
  'hf_[A-Za-z0-9]{20,}'
  'gh[pousr]_[A-Za-z0-9]{20,}'
  'nvapi-[A-Za-z0-9_-]{20,}'
  'ark-[A-Za-z0-9][A-Za-z0-9-]{12,}'
  'sk-[A-Za-z0-9_-]{24,}'
  'AKIA[0-9A-F]{16}'
  'xox[baprs]-[A-Za-z0-9-]{10,}'
  '-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'
  # 第 9 条（v4 扩 auth_token/authorization；H-12 扩 app_secret/feishu_app_secret
  # 赋值形态；S-2 补 name 后可选收尾引号，覆盖 "access_token" : "..." JSON 形态）：
  # 与 orchestrator/common/secrets.py::assigned_secret_kv 同源；
  # shell ERE 无反向引用，左右引号各自独立（Python 版用 \5 锁同引号，更严）。
  '(api[_-]?key|access[_-]?token|auth[_-]?token|authorization|app[_-]?secret|feishu[_-]?app[_-]?secret)(["'"'"']?)([[:space:]]{0,5})[:=]([[:space:]]{0,5})["'"'"'][A-Za-z0-9_./+-]{24,}["'"'"']'
  # 第 10 条（H-12）：飞书自建应用 app_id（cli_ 前缀）；与
  # orchestrator/common/secrets.py::lark_cli_app_id 同源。
  'cli_[a-z0-9]{16,}'
)
hits=0
while IFS= read -r f; do
  # 跳过二进制与本扫描脚本自身的模式定义
  [ "$f" = "scripts/verification/scan_secrets.sh" ] && continue
  if grep -Iq . "$f" 2>/dev/null; then
    for p in "${PATTERNS[@]}"; do
      # -i：第 9 条需同时覆盖 Authorization/ANTHROPIC_AUTH_TOKEN 等大小写形态
      if m=$(grep -niE "$p" "$f" 2>/dev/null); then
        echo "[secret-scan HIT] $f :: $m"
        hits=$((hits+1))
      fi
    done
  fi
done < <(printf '%s\n' "${FILES[@]}")

if [ "$hits" -gt 0 ]; then
  echo "[secret-scan] 发现 $hits 处疑似密钥，禁止 commit"
  exit 1
fi
echo "[secret-scan OK] 扫描 ${#FILES[@]} 个文件，无密钥命中"
