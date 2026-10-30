#!/usr/bin/env bash
# Phase 2.5 Task4 AC-T4-18：构建 agentic run_cmd 兄弟容器镜像并留证。
#
# 硬约束（测试逐字段断言，缺项必红）：
# - SIMPRAXIS_USE_PROXY=0 必须显式给出：直连构建，防执行期走代理出网；
# - 构建网络面：docker build --network=none --pull=false（基础镜像只允许
#   本机已存 digest，构建期零拉取、零 apt/pip）；
# - 证据 sandbox/docker/agentic_build_evidence.json：基础/产物 digest、
#   Dockerfile+manifest 的 sha256、运行网络面（--network=none）烟囱结果。
# 网络阻断时本脚本非零退出并 STOP 报 LEAD——禁止改 digest pin、禁止松网络。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DOCKER_DIR="$REPO_ROOT/sandbox/docker"
DOCKERFILE="$DOCKER_DIR/Dockerfile.agentic"
MANIFEST="$DOCKER_DIR/agentic_wheel_manifest.json"
EVIDENCE="$DOCKER_DIR/agentic_build_evidence.json"
IMAGE_TAG="sim-praxis/agentic:phase25-t4"
PY=/mnt/venvs/simpraxis/bin/python

if [[ "${SIMPRAXIS_USE_PROXY:-}" != "0" ]]; then
  echo "STOP: 必须显式 SIMPRAXIS_USE_PROXY=0（AC-T4-18 直连构建），当前=" \
       "'${SIMPRAXIS_USE_PROXY:-<unset>}'" >&2
  exit 2
fi
[[ -f "$DOCKERFILE" && -f "$MANIFEST" ]] || { echo "缺 Dockerfile/manifest" >&2; exit 2; }

FROM_REF="$($PY - "$DOCKERFILE" <<'PYEOF'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r"^FROM\s+(\S+)", text, re.M)
if not m or "@sha256:" not in m.group(1):
    sys.exit("Dockerfile.agentic 必须 FROM <ref>@sha256:<64hex>")
print(m.group(1))
PYEOF
)"
MANIFEST_BASE="$($PY -c 'import json,sys; print(json.load(open(sys.argv[1]))["base_image"])' "$MANIFEST")"
[[ "$FROM_REF" == "$MANIFEST_BASE" ]] || {
  echo "STOP: Dockerfile FROM 与 wheel manifest base_image 不一致" >&2; exit 2; }

# 构建网络面与运行网络面分离：构建即禁网、禁拉取。
docker build --network=none --pull=false \
  -f "$DOCKERFILE" -t "$IMAGE_TAG" "$DOCKER_DIR"

BASE_ID="$(docker image inspect --format '{{.Id}}' "$FROM_REF")"
IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$IMAGE_TAG")"

# 运行网络面烟囱：生产 argv 形态（--network=none/只读根/cap-drop/venv ro）。
SMOKE_WORK="$(mktemp -d)"
trap 'rm -rf "$SMOKE_WORK"' EXIT
set +e
SMOKE_OUT="$(docker run --rm -i --network=none --user 1000:1000 \
  --cap-drop ALL --security-opt no-new-privileges --read-only \
  --pids-limit 256 --memory 4g \
  --tmpfs /tmp:rw,size=64m,mode=1777 \
  -v "$SMOKE_WORK:/work" -v /mnt/venvs/simpraxis:/opt/venv:ro -w /work \
  --entrypoint /opt/venv/bin/python "$IMAGE_TAG" \
  -c 'import sys, smolagents; print("PY", sys.version.split()[0]); print("SM", smolagents.__version__)' 2>&1)"
SMOKE_RC=$?
set -e

SMOKE_OUT="$SMOKE_OUT" SMOKE_RC="$SMOKE_RC" IMAGE_TAG="$IMAGE_TAG" \
IMAGE_ID="$IMAGE_ID" FROM_REF="$FROM_REF" BASE_ID="$BASE_ID" \
DOCKERFILE="$DOCKERFILE" MANIFEST="$MANIFEST" \
SIMPRAXIS_USE_PROXY=0 "$PY" - "$EVIDENCE" <<'PYEOF'
import hashlib, json, os, sys
from datetime import datetime, timezone
evidence = sys.argv[1]

def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()

image_id = os.environ["IMAGE_ID"]
rec = {
    "schema": "simpraxis.agentic_image_evidence.v1",
    "ts_iso": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "image_tag": os.environ["IMAGE_TAG"],
    # M4（2026-10-30 评审）：引用必须是 docker 可解析的 tag@sha256:<hex>
    # 形；tag@<裸 hex> 是 invalid reference（宿主侧实证 rc125）。
    "image_ref_digest": os.environ["IMAGE_TAG"] + "@sha256:" + image_id.removeprefix("sha256:"),
    "image_id": image_id,
    "base_ref": os.environ["FROM_REF"],
    "base_image_id": os.environ["BASE_ID"],
    "build_plane": {
        "simpraxis_use_proxy": "0",
        "network": "none",
        "pull": "false (--network=none --pull=false)",
        "wheels_installed": 0,
    },
    "runtime_plane": {
        "network": "--network=none",
        "read_only_root": True,
        "cap_drop": "ALL",
        "no_new_privileges": True,
        "venv_mount": "/mnt/venvs/simpraxis:/opt/venv:ro",
    },
    "runtime_smoke": {"rc": int(os.environ["SMOKE_RC"]),
                      "output": os.environ["SMOKE_OUT"]},
    "dockerfile_sha256": sha(os.environ["DOCKERFILE"]),
    "wheel_manifest_sha256": sha(os.environ["MANIFEST"]),
}
with open(evidence, "w", encoding="utf-8") as fh:
    json.dump(rec, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
print("evidence:", evidence)
PYEOF

if [[ "$SMOKE_RC" -ne 0 ]]; then
  echo "STOP: 运行网络面烟囱 rc=$SMOKE_RC（见证据），不松 pin/不松网络" >&2
  exit 3
fi
echo "built $IMAGE_TAG ($IMAGE_ID) from $FROM_REF"
