#!/usr/bin/env bash
# S0-8 / WP-P：ProductBundle v1 一键还原（校验→软链→环境→自检）。
#
# 用法：
#   bash scripts/kb/bootstrap.sh <product-bundle> <target-root> [--kb-root <活 KB 仓>]
#
# 步骤（任一失败即 exit 非零，不留半还原目录）：
#   1. verify_bundle.py 全过（文件/行数/维度/SHA/闭源清单/内容面零泄漏）；
#   2. 还原布局：<target>/data/lancedb -> bundle/tables；
#                <target>/kb/{knowledge,solutions} -> bundle/content/*；
#                <target>/kb/data -> <target>/data；
#   3. 环境注入：写 <target>/env.sh（SIMPRAXIS_LANCEDB_URI / KB_ROOT）；
#   4. 自检：表行数/维度对账 + 内容树哈希（verify 已复算，这里再开表核行）；
#      给了 --kb-root 时另跑 check_solutions（内容层分层校验）。
set -euo pipefail

PY="${PY:-/mnt/venvs/simpraxis/bin/python}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

BUNDLE="${1:?用法: bootstrap.sh <product-bundle> <target-root> [--kb-root <KB>]}"
TARGET="${2:?用法: bootstrap.sh <product-bundle> <target-root> [--kb-root <KB>]}"
shift 2
KB_ROOT_LIVE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --kb-root) KB_ROOT_LIVE="${2:?}"; shift 2 ;;
    *) echo "[bootstrap STOP] 未知参数：$1" >&2; exit 2 ;;
  esac
done

if [[ -e "$TARGET" ]]; then
  echo "[bootstrap STOP] 目标已存在（拒绝覆盖）：$TARGET" >&2
  exit 2
fi

echo "[bootstrap] 1/4 校验 $BUNDLE"
"$PY" "$REPO_ROOT/scripts/kb/verify_bundle.py" "$BUNDLE" --engine-allow-ancestor

echo "[bootstrap] 2/4 还原布局 -> $TARGET"
umask 077
STAGE="$TARGET.tmp.$$"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/kb" "$STAGE/data"
ln -s "$(cd "$BUNDLE" && pwd)/tables" "$STAGE/data/lancedb"
ln -s "$(cd "$BUNDLE" && pwd)/content/knowledge" "$STAGE/kb/knowledge"
ln -s "$(cd "$BUNDLE" && pwd)/content/solutions" "$STAGE/kb/solutions"
mv -T "$STAGE" "$TARGET"
trap - EXIT
ln -s "$TARGET/data" "$TARGET/kb/data"  # 指向最终路径（不是临时目录）

echo "[bootstrap] 3/4 环境注入 -> $TARGET/env.sh"
cat > "$TARGET/env.sh" <<EOF
# 由 scripts/kb/bootstrap.sh 生成（ProductBundle v1）
export SIMPRAXIS_LANCEDB_URI="$TARGET/data/lancedb"
export KB_ROOT="$TARGET/kb"
EOF
chmod 600 "$TARGET/env.sh"

echo "[bootstrap] 4/4 自检"
"$PY" - "$BUNDLE" "$TARGET" <<'PYEOF'
import json, sys
from pathlib import Path
import lancedb

bundle, target = Path(sys.argv[1]), Path(sys.argv[2])
manifest = json.loads((bundle / "bundle_manifest.json").read_text(encoding="utf-8"))
if manifest.get("tier") != "product":
    raise SystemExit(f"[bootstrap FAIL] tier={manifest.get('tier')!r} 不是 product")
db = lancedb.connect(str(target / "data" / "lancedb"))
for table, meta in manifest["tables"].items():
    rows = db.open_table(table).count_rows()
    if rows != meta["rows"]:
        raise SystemExit(f"[bootstrap FAIL] {table} 行数 {rows} != {meta['rows']}")
    schema = db.open_table(table).schema
    if meta.get("vector_dim") and "vector" in schema.names:
        dim = int(schema.field("vector").type.list_size)
        if dim != meta["vector_dim"]:
            raise SystemExit(f"[bootstrap FAIL] {table} 维度 {dim} != {meta['vector_dim']}")
print(f"[bootstrap] 表对账 OK：{len(manifest['tables'])} 表")
PYEOF

test -e "$TARGET/kb/data/lancedb" || { echo "[bootstrap FAIL] kb/data 软链悬空" >&2; exit 2; }

if [[ -n "$KB_ROOT_LIVE" ]]; then
  echo "[bootstrap] 内容层分层校验（check_solutions --kb-root $KB_ROOT_LIVE）"
  "$PY" "$REPO_ROOT/scripts/kb/check_solutions.py" --kb-root "$KB_ROOT_LIVE"
fi

echo "[bootstrap OK] $TARGET（source $TARGET/env.sh 注入环境）"
