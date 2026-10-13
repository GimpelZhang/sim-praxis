#!/usr/bin/env python3
"""探针 D：Gate2 原生 mp4 真实上传/发送 + 30MB 硬上限闸门。

两阶段（宿主机不装 ffmpeg，飞书凭据不进镜像）：
  1) --build-container：只【生成】容器内合成脚本与 compose 命令文本（不执行 docker）；
     产物目录 $SIMPRAXIS_RUN_DIR/phase1/task1_feishu/probe_d/（宿主经挂载同路径可读）。
  2) 默认宿主机阶段：files 上传容器产物 mp4 → images 首帧封面 → media 发送
     （MEDIA_CONTENT_SHAPE 两种 content 形态自动尝试）→ 终端人工确认可播放 →
     构造 31MB 随机字节临时文件（写 /mnt 证据目录，禁止入仓）断言 files 接口拒绝并
     记录错误码 → 删除临时文件 → 落 probe_d_media.json，exit 0/2。

运行：
  python -m orchestrator.feishu_gateway.probes.probe_d_video --build-container
  cd sandbox && docker compose build sim-sandbox   # 按生成 commands.txt 执行
  docker compose run --rm sim-sandbox bash /root/runs/phase1/task1_feishu/probe_d/make_probe_video.sh
  python -m orchestrator.feishu_gateway.probes.probe_d_video
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

from ..client import FeishuAPIError, gateway_from_env
from ._evidence import (
    confirm,
    now_iso,
    print_checklist,
    write_evidence,
)

OVERSIZE_BYTES = 31 * 1024 * 1024   # 31MB > 30MB files 接口硬上限

# 容器内合成脚本（imageio 2.36.1 写帧 + 镜像内 ffmpeg x264 crf23 yuv420p + 首帧封面）。
# 黑底 + 随机噪点带（保证 ≥1MB 熵）+ 白字帧号/时间戳，4s @24fps，1280x720。
_CONTAINER_SCRIPT = r'''#!/usr/bin/env bash
set -euo pipefail
mkdir -p /root/runs/phase1/task1_feishu/probe_d
python3 - <<'PY'
import numpy as np
import imageio.v2 as imageio

out = "/root/runs/phase1/task1_feishu/probe_d/probe_d_synthetic.mp4"
fps, seconds, w, h = 24, 4, 1280, 720
rng = np.random.default_rng(20261013)
writer = imageio.get_writer(
    out, fps=fps, codec="libx264", quality=8, macro_block_size=None,
    output_params=["-pix_fmt", "yuv420p", "-crf", "23"])
for i in range(fps * seconds):
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    # 每帧变化的随机噪点带提供足够熵（目标 ≥1MB）
    frame[80:640, 80:1200] = rng.integers(0, 255, (560, 1120, 3), dtype=np.uint8)
    t = i / fps
    label = f"SimPraxis probeD frame {i:03d}  t={t:0.2f}s  crf23 yuv420p"
    # 简单 5x7 点阵白字成本过高；用顶部白色计时条 + 帧号灰阶条代替可读时间戳
    frame[0:12, :min(w, int(w * (i + 1) / (fps * seconds)))] = 255
    frame[h - 12:h, :min(w, int(w * ((i % fps) + 1) / fps))] = 200
    writer.append_data(frame)
writer.close()
print("VIDEO_WRITTEN", out)
PY
ffmpeg -y -i /root/runs/phase1/task1_feishu/probe_d/probe_d_synthetic.mp4 \
  -frames:v 1 -an /root/runs/phase1/task1_feishu/probe_d/probe_d_cover.png
ls -l /root/runs/phase1/task1_feishu/probe_d/
'''


def _run_dir() -> Path:
    root = Path(os.environ.get("SIMPRAXIS_RUN_DIR",
                               "/mnt/artifacts/simpraxis/runs"))
    d = root / "phase1" / "task1_feishu" / "probe_d"
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_container_artifacts() -> int:
    d = _run_dir()
    script = d / "make_probe_video.sh"
    script.write_text(_CONTAINER_SCRIPT, encoding="utf-8")
    os.chmod(script, 0o755)
    commands = d / "commands.txt"
    commands.write_text(
        "# 探针 D 容器合成步骤（凭据不进容器；在仓库根执行）\n"
        "cd /home/vipuser/sim-praxis/sandbox\n"
        "SIMPRAXIS_USE_PROXY=0 docker compose build sim-sandbox 2>&1 | "
        "tee \"$SIMPRAXIS_RUN_DIR/build-phase1-probe-$(date +%Y%m%d-%H%M%S).log\"\n"
        "docker compose run --rm sim-sandbox bash "
        "/root/runs/phase1/task1_feishu/probe_d/make_probe_video.sh\n"
        "docker compose down\n"
        "# 产物：probe_d_synthetic.mp4（≥1MB/4s）与 probe_d_cover.png\n",
        encoding="utf-8")
    print_checklist("探针 D / 阶段 1：容器合成脚本已生成（本机未执行 docker）", [
        f"检查脚本：{script}",
        f"按命令文本执行：{commands}",
        "容器跑完后在宿主机执行（不带参数）：",
        "  /mnt/venvs/simpraxis/bin/python -m "
        "orchestrator.feishu_gateway.probes.probe_d_video",
        "收尾：docker compose down；docker ps -a 无 sim_praxis_sandbox；显存 ≤200MiB",
    ])
    return 0


def run_host_upload(video: Path, cover: Path, duration_ms: int) -> int:
    print_checklist("探针 D / 阶段 2：宿主机上传与 media 真实发送", [
        "等待机器人在 P2P 发出视频消息",
        "在飞书里打开视频，确认：可以播放、时长约 4s、封面为首帧、非黑屏损坏",
        "随后终端会要求你按 y 确认；之后脚本自动做 31MB 超限闸门验证",
    ])
    try:
        gw = gateway_from_env(start_ws=False)
    except RuntimeError as exc:
        print(f"[probe-d] {exc}", file=sys.stderr)
        return 2

    checks: list[dict] = []
    file_key = image_key = None
    media_shape = media_message_id = None
    try:
        if not video.exists():
            print(f"[probe-d] 缺少容器产物 {video}（先跑阶段 1）", file=sys.stderr)
            return 2
        size = video.stat().st_size
        try:
            file_key = gw.upload_file(video, duration_ms=duration_ms)
            checks.append({"item": "files 上传 mp4（≥1MB）",
                           "expected": "file_key 非空", "actual": file_key,
                           "passed": bool(file_key)})
        except FeishuAPIError as exc:
            checks.append({"item": "files 上传 mp4", "expected": "成功",
                           "actual": f"code={exc.code} msg={exc.msg}",
                           "passed": False})
        if file_key and cover.exists():
            try:
                image_key = gw.upload_image(cover)
                checks.append({"item": "images 上传首帧封面",
                               "expected": "image_key 非空", "actual": image_key,
                               "passed": bool(image_key)})
            except FeishuAPIError as exc:
                checks.append({"item": "images 上传首帧封面", "expected": "成功",
                               "actual": f"code={exc.code} msg={exc.msg}",
                               "passed": False})
        if file_key:
            try:
                media = gw.send_media(gw.owner_open_id, file_key, image_key)
                media_message_id = media["message_id"]
                media_shape = media["shape"]
                checks.append({"item": "media 消息发送",
                               "expected": "message_id 非空",
                               "actual": media_message_id,
                               "passed": bool(media_message_id)})
            except FeishuAPIError as exc:
                checks.append({"item": "media 消息发送", "expected": "成功",
                               "actual": f"code={exc.code} msg={exc.msg}",
                               "passed": False})

        playable = bool(media_message_id) and confirm(
            f"media 形态={media_shape}。请确认视频在飞书可播放、封面为首帧")
        checks.append({"item": "人工确认可播放/封面正确", "expected": True,
                       "actual": playable, "passed": playable})

        # ---- 31MB 硬上限闸门：随机字节临时文件只写 /mnt，结束必删，禁止入仓 ----
        oversize = (gw.evidence_dir /
                    f"probe_d_oversize_31mb_{uuid.uuid4().hex[:8]}.bin")
        oversize_rejected = False
        oversize_code = oversize_msg = None
        try:
            with oversize.open("wb") as fh:
                fh.write(os.urandom(OVERSIZE_BYTES))
            try:
                gw.upload_file(oversize, duration_ms=1000)
                oversize_rejected = False  # 接口竟然接受：闸门失效，探针失败
                oversize_msg = "31MB 文件被接口接受（与 30MB 硬上限文档矛盾）"
            except FeishuAPIError as exc:
                oversize_rejected = True
                oversize_code, oversize_msg = exc.code, exc.msg
        finally:
            if oversize.exists():
                oversize.unlink()
        checks.append({"item": "31MB 文件被 files 接口拒绝（30MB 硬上限）",
                       "expected": "code != 0",
                       "actual": (f"拒绝 code={oversize_code} msg={oversize_msg}"
                                  if oversize_rejected else oversize_msg),
                       "passed": oversize_rejected})
        checks.append({"item": "31MB 临时文件已删除", "expected": True,
                       "actual": not oversize.exists(),
                       "passed": not oversize.exists()})

        passed = all(c["passed"] for c in checks)
        payload = {
            "probe": "d_native_media",
            "ran_at": now_iso(),
            "video": str(video), "cover": str(cover),
            "video_size": size, "duration_ms": duration_ms,
            "file_key": file_key, "image_key": image_key,
            "media_message_id": media_message_id,
            "media_content_shape_accepted": media_shape,
            "media_content_attempts": gw.MEDIA_CONTENT_SHAPE["attempt_order"],
            "oversize_31mb": {"rejected": oversize_rejected,
                              "code": oversize_code, "msg": oversize_msg,
                              "temp_deleted": not oversize.exists()},
            "checks": checks,
            "manual_confirmed": playable,
            "passed": passed,
        }
        path = write_evidence("probe_d_media.json", payload)
        print(f"[probe-d] passed={passed}；证据 {path}", flush=True)
        return 0 if passed else 2
    finally:
        gw.close()


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(description="探针 D：原生 mp4 + 30MB 闸门")
    p.add_argument("--build-container", action="store_true",
                   help="只生成容器内合成脚本与 compose 命令文本，不执行 docker")
    d = _run_dir()
    p.add_argument("--video",
                   default=str(d / "probe_d_synthetic.mp4"))
    p.add_argument("--cover",
                   default=str(d / "probe_d_cover.png"))
    p.add_argument("--duration-ms", type=int, default=4000)
    args = p.parse_args(argv)
    if args.build_container:
        return build_container_artifacts()
    return run_host_upload(Path(args.video), Path(args.cover), args.duration_ms)


if __name__ == "__main__":
    sys.exit(main())
