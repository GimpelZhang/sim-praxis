#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail() { echo "[PREFLIGHT FAIL] $*" >&2; exit 1; }

# 1. GPU：必须是 A800 40GB，基线显存低位
line=$(nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader)
echo "$line"
echo "$line" | grep -q 'NVIDIA A800-SXM4-40GB' || fail "GPU 型号或显存档位与事实表不符（期望 A800-SXM4-40GB）"
echo "$line" | awk -F',' '{gsub(/[^0-9]/,"",$2); if ($2+0 != 40960) exit 1}' || fail "显存总量应为 40960 MiB"

# 2. Docker / toolkit / data-root
docker version --format '{{.Server.Version}}' | grep -Eq '^29\.' || fail "Docker 版本异常"
docker info --format '{{.DockerRootDir}}' | grep -q '^/mnt/docker-data' || fail "docker data-root 不在 /mnt/docker-data"
command -v nvidia-ctk >/dev/null || fail "nvidia-container-toolkit 缺失"
[ -z "$(docker ps -aq)" ] && echo "no running/existing containers (expected at Phase0 start)" || \
  echo "WARN: 存在既有容器，确认非本计划残留即可"

# 3. 容器 GPU 直通（拉取约 200MB base 镜像，走 dockerhub；若已在 /mnt 缓存则秒回）
#    拉取失败必须非零退出并给出可执行的排障路径，不允许静默跳过。
# 执行期适配（2026-10-12）：原写法 `docker run ... | grep -q` 在 set -o pipefail 下
# 确定性触发 SIGPIPE——grep -q 首行命中即关闭管道，docker CLI 因 broken pipe 非零退出，
# GPU 直通本身正常（直跑 nvidia-smi 实测 rc=0）。改为先捕获再 grep，断言语义不变。
cuda_smi="$(docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi 2>/tmp/p0_cuda_pull.err)" \
  || cuda_smi=""
if ! printf '%s\n' "$cuda_smi" | grep -q 'NVIDIA A800-SXM4-40GB'; then
  cat /tmp/p0_cuda_pull.err >&2 || true
  fail "容器 GPU 直通/base 镜像拉取失败。处置（按序）：
  (a) docker pull nvidia/cuda:12.4.1-base-ubuntu22.04 重试 3 次（指数退避）；
  (b) 若为 dockerhub 网络问题：docker pull docker.1ms.run/nvidia/cuda:12.4.1-base-ubuntu22.04
      && docker tag docker.1ms.run/nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia/cuda:12.4.1-base-ubuntu22.04；
  (c) 若容器内无 GPU：nvidia-ctk runtime configure --runtime=docker 后重启 docker（HITL，需 sudo）；
  (d) 以上均失败则停下报告，禁止换用其它 base 镜像或跳过本断言。"
fi

# 4. 磁盘
avail_mnt=$(df -m --output=avail /mnt | tail -1)
[ "$avail_mnt" -gt 102400 ] || fail "/mnt 可用空间不足 100GB"
avail_root=$(df -m --output=avail / | tail -1)
[ "$avail_root" -gt 40960 ] || fail "/ 可用空间不足 40GB"

# 5. 两个仓库状态
kb_root="${KB_ROOT:-/home/vipuser/embodied-ai-sim-knowledge-base}"
git -C /home/vipuser/sim-praxis rev-parse --abbrev-ref HEAD | grep -qx main || fail "sim-praxis 不在 main"
git -C "$kb_root" diff-index --quiet HEAD \
  || fail "知识库仓库不干净——Task0b 起内容层允许经提交追加，但工作区必须干净"
# Task0b 回填：v4.1 M3 旧断言「HEAD 全 SHA == aac53313」改为分层不变量——
# HEAD 必须是迁移提交（solutions/ 首次写入 c5712e9f）的后代，且 knowledge/
# 32-md sha256 集逐字等于 aac53313 基线（只增不改）。真值单一源在
# memory_flywheel/kb_pins.py（scripts/01_preflight.sh、ingest_kb、e2e test_05 共用）。
KB_ROOT="$kb_root" /mnt/venvs/simpraxis/bin/python -m memory_flywheel.kb_pins --kb-root "$kb_root" \
  || fail "知识库分层不变量失败（HEAD 非迁移提交后代或 knowledge/ 哈希集偏离 aac53313 基线，见上）"

# 6. Python 与网络镜像
/usr/bin/python3 --version | grep -q '3.10' || fail "系统 Python 非 3.10"
/mnt/venvs/simpraxis/bin/python --version | grep -q '3.10' || fail "simpraxis venv 异常"
curl -fsS --max-time 10 -o /dev/null https://pypi.tuna.tsinghua.edu.cn/simple/pip/ || fail "清华 pip 镜像不可达"
command -v ffprobe >/dev/null && ffprobe -version 2>/dev/null | head -1 | grep -q '^ffprobe' \
  || fail "宿主机缺 ffprobe（Milestone 1080p 视频断言需要）"

# 7. 凭据可解析（不打印值）
# shellcheck disable=SC1091
source scripts/00_env.sh

echo "[PREFLIGHT OK] 全部事实复核通过"
