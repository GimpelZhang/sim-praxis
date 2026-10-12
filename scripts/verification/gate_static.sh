#!/usr/bin/env bash
# 交付前静态门禁：语法/解析 + 密钥扫描 + 探针纯函数数学 + 网关 monkeypatch 单测。
# 任一失败即非零退出。v4.1 M4：六步（v4 散文声称含网关单测、脚本实缺，已补第 6 步）。
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
VENV_PY=/mnt/venvs/simpraxis/bin/python

echo "== [gate 1/6] bash -n 全部交付 shell =="
# m-a：门禁在 git add 之前运行，必须同时覆盖 tracked 与未跟踪新文件（-co --exclude-standard）
mapfile -t SH < <(git ls-files -co --exclude-standard '*.sh' ':!*.sample')
[ "${#SH[@]}" -gt 0 ] || { echo "[gate] 尚无 .sh，跳过"; }
for f in "${SH[@]:-}"; do [ -z "$f" ] && continue; bash -n "$f" && echo "  ok $f"; done

echo "== [gate 2/6] py_compile 全部交付 python =="
mapfile -t PY < <(git ls-files -co --exclude-standard '*.py')
if [ "${#PY[@]}" -gt 0 ]; then
  "$VENV_PY" -m py_compile "${PY[@]}"
  echo "  ok ${#PY[@]} files"
else echo "  尚无 .py，跳过"; fi

echo "== [gate 3/6] YAML/JSON 可解析 =="
"$VENV_PY" - <<'PYEOF'
import glob, json, pathlib, sys
import yaml
bad = []
# m-f：只机检交付文件；运营态 .omc/（状态 YAML/JSON）与 .git/ 不入库、不机检，
# 否则 recursive glob 会把它们当交付文件解析而误伤。.sample.yaml 逻辑不变。
def _is_delivery(f):
    return not (f.startswith(".omc/") or f.startswith(".git/"))
for f in glob.glob("**/*.yaml", recursive=True) + glob.glob("**/*.yml", recursive=True):
    if not _is_delivery(f):
        continue
    if f.endswith((".sample.yaml",)):  # 模板同样必须可解析
        pass
    try:
        yaml.safe_load(pathlib.Path(f).read_text(encoding="utf-8"))
    except Exception as e:
        bad.append(f"YAML {f}: {e}")
for f in glob.glob("**/*.json", recursive=True):
    if not _is_delivery(f):
        continue
    try:
        json.loads(pathlib.Path(f).read_text(encoding="utf-8"))
    except Exception as e:
        bad.append(f"JSON {f}: {e}")
if bad:
    print("\n".join(bad)); sys.exit(1)
print("  ok yaml/json parse")
PYEOF

echo "== [gate 4/6] 交付文件密钥自扫（9 条正则，期望 0 命中，无 allowlist）=="
bash scripts/verification/scan_secrets.sh

echo "== [gate 5/6] 探针纯函数数学（clearColor 字节/中心裁剪包络/相对软链）=="
if [ -f tests/test_probe_math.py ]; then
  "$VENV_PY" -m pytest tests/test_probe_math.py -q
elif git ls-files --error-unmatch scripts/verification/verify_egl_real.py >/dev/null 2>&1 \
     || [ -f scripts/verification/verify_egl_real.py ]; then
  echo "[gate FAIL] 探针已存在却没有 tests/test_probe_math.py——禁止跳过纯函数数学测试" >&2
  exit 1
else
  echo "  tests/test_probe_math.py 尚未创建（Task 1 起强制；届时缺失即本门禁失败）"
fi

echo "== [gate 6/6] 网关 monkeypatch 单测（不联网不付费；v4.1 18 个）=="
if [ -f tests/test_gateway.py ]; then
  "$VENV_PY" -m pytest tests/test_gateway.py -q
elif git ls-files --error-unmatch orchestrator/gateway/router.py >/dev/null 2>&1 \
     || [ -f orchestrator/gateway/router.py ]; then
  echo "[gate FAIL] 网关代码已存在却没有 tests/test_gateway.py——禁止跳过网关门禁（M4）" >&2
  exit 1
else
  echo "  tests/test_gateway.py 尚未创建（Task 2 起强制；届时缺失即本门禁失败）"
fi
echo "[STATIC GATE OK]"
