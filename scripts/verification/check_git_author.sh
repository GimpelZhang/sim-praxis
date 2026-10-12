#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
author=$(git log -1 --format='%an <%ae>')
expected="Junchuan Zhang <zjunchuan@gmail.com>"
if [ "$author" != "$expected" ]; then
  echo "[author FAIL] 最近 commit author='$author'，期望='$expected'" >&2
  exit 1
fi
echo "[author OK] $author"
