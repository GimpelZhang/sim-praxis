#!/usr/bin/env python3
"""容器构建期 pip check 白名单（Task8 D2 决策，用户书面裁定 B′，2026-10-15）。

torch 2.4.1+cu124 的 METADATA 精确钉
nvidia-cuda-nvrtc-cu12==12.4.99 / nvidia-nvjitlink-cu12==12.4.99，
而 genesis-world 1.3.3 硬钉的 gs-madrona==0.0.10 要求两库 >=12.8。
两个方向都无法让 pip check 归零（元数据矛盾，非安装器问题），
用户裁定保留 12.9.86；双向运行时实测等价（真 A800 容器探针逐项同值），
故构建期只允许这两条已知 torch 冲突，出现任何其他冲突立即失败。

证据：
  phase1/task8_track2/install_compat/DECISION_D2_cuda_libs.json
  phase1/task8_track2/install_compat/RUNTIME_CONFLICT_EVIDENCE.md
"""
import subprocess
import sys

EXPECTED = (
    ("torch 2.4.1", "nvidia-cuda-nvrtc-cu12==12.4.99",
     "nvidia-cuda-nvrtc-cu12 12.9.86"),
    ("torch 2.4.1", "nvidia-nvjitlink-cu12==12.4.99",
     "nvidia-nvjitlink-cu12 12.9.86"),
)

proc = subprocess.run([sys.executable, "-m", "pip", "check"],
                      text=True, capture_output=True)
lines = sorted(l for l in proc.stdout.splitlines() if l.strip())

if len(lines) != len(EXPECTED):
    sys.stderr.write(
        f"pip check 出现 {len(lines)} 条冲突，白名单只允许 "
        f"{len(EXPECTED)} 条已知 torch/12.9.86 冲突：\n"
        + "\n".join(lines) + "\n")
    sys.exit(1)

for line, (a, b, c) in zip(lines, EXPECTED):
    if not (a in line and b in line and c in line):
        sys.stderr.write(f"非白名单冲突行：{line!r}\n")
        sys.exit(1)

print("pip check whitelist OK：仅 2 条已知冲突 "
      "（D2-T8-CUDA-USERSPACE-LIBS B′，记录在案）")
