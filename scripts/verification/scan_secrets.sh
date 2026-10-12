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
  # v4：关键字扩 auth_token/authorization（大小写不敏感），其余结构不变；仍为第 9 条
  '(api[_-]?key|access[_-]?token|auth[_-]?token|authorization)([[:space:]]{0,5})[:=]([[:space:]]{0,5})["'"'"'][A-Za-z0-9_./+-]{24,}["'"'"']'
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
