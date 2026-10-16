#!/usr/bin/env python3
"""Task5 标定片合成（详细计划 L526：1 好片 + 1 注入穿模/抖动的坏片）。

明确这是**标定仪器**而非仿真产物：2D 侧视示意动画（PIL 绘制，ground truth
由脚本注入），用于在真实 rollout（Task7）前检验 Rung4 rubric 的检出力。
残余风险（2D 卡通 ≠ 真实渲染）写入 calibration_6q.json；常态预算 4 次
含「Task7 真实片复拍/坏片复测」余量，届时以真实片重标定。

时序（60 帧 @60 fps，1.0 s）：
  0-19  夹爪竖直下降（指张开，gap=60）
  20-29 夹爪闭合（gap 60→48 = 块宽 40 + 指宽 8，指内面与块侧面齐平接触）
  30-49 夹爪夹持方块上移（夹爪 y 始终与块中心对齐，随块一起上升）
  50-59 悬停（块被夹爪持在桌面上方）
坏片在 30-45 注入：夹爪/方块交叠 + 方块陷入桌面（穿模 6 px），30-50 方块
位置高频 ±4 px 随机抖动（固定种子，帧帧可复现）。

标定有效性纪律（锁内适配，2026-10-14 首轮实呼后修正）：
- 帧内只印中性「frame NN」，绝不印 kind 字样或 INJECTED 标记——不得向
  Rung4 裁判泄漏 ground truth；真值只在 ground_truth.json。
- 好片几何必须真正无交叠（首轮 gap=44 导致指尖插入块侧 2px，VLM 如实
  报 q1/q3/q6=no，证明旧夹具是坏的好片，已废）。
- 标准答案按题给：坏片注入只覆盖 q1–q5（末帧 59 无注入，q6 真值是 yes），
  不使用「坏片六题全 defect」的粗暴映射。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from orchestrator.common.fsutil import atomic_write_text



W, H = 320, 240
FPS = 60
N_FRAMES = 60
CONTACT_FRAME = 30          # 夹爪带物起升/接触时刻

TABLE_Y = 180
BLOCK_W = BLOCK_H = 40
JAW_W, JAW_H = 8, 30


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _block_y(fi: int) -> float:
    # 起升前段方块在桌面上；30 帧起随夹爪上移 40 px
    if fi < 30:
        return TABLE_Y - BLOCK_H / 2
    if fi < 50:
        return _lerp(TABLE_Y - BLOCK_H / 2, TABLE_Y - BLOCK_H / 2 - 40,
                     (fi - 30) / 20)
    return TABLE_Y - BLOCK_H / 2 - 40


JAW_OPEN_GAP = 60.0
JAW_CLOSED_GAP = float(BLOCK_W + JAW_W)   # 48：指内面与块侧面齐平


def _gripper_y(fi: int) -> float:
    # 夹爪中心始终对齐块中心：jaw 矩形顶 gy，高 JAW_H，故 gy = by - JAW_H/2
    if fi < 20:
        return _lerp(60.0, _block_y(0) - JAW_H / 2, fi / 20)
    return _block_y(min(fi, 29)) - JAW_H / 2


def _jaw_gap(fi: int) -> float:
    if fi < 20:
        return JAW_OPEN_GAP
    if fi < 30:
        return _lerp(JAW_OPEN_GAP, JAW_CLOSED_GAP, (fi - 20) / 10)
    return JAW_CLOSED_GAP


def render_clip(kind: str):
    """kind: 'good' | 'bad'。返回 (frames[PIL.Image], meta)。"""
    import random
    from PIL import Image, ImageDraw

    if kind not in ("good", "bad"):
        raise ValueError("kind 必须是 good|bad")
    rng = random.Random(20261014)          # 固定种子：坏片抖动可复现
    frames = []
    block_cx = W / 2
    for fi in range(N_FRAMES):
        img = Image.new("RGB", (W, H), (245, 245, 248))
        d = ImageDraw.Draw(img)
        # 桌面
        d.rectangle([0, TABLE_Y, W, H], fill=(180, 180, 190))
        d.line([0, TABLE_Y, W, TABLE_Y], fill=(80, 80, 90), width=2)

        by = _block_y(fi)
        cx = block_cx
        if kind == "bad" and 30 <= fi <= 50:
            cx += rng.choice([-4, -2, 3, 4, -3, 2])   # 高频 ±4 px 抖动
        pen = 0
        if kind == "bad" and 30 <= fi <= 45:
            # 穿模注入：方块下压 6 px（陷入桌面），夹爪插入方块 6 px
            by += 6
            pen = 6

        # 方块（红）
        x0, y0 = cx - BLOCK_W / 2, by - BLOCK_H / 2
        d.rectangle([x0, y0, x0 + BLOCK_W, y0 + BLOCK_H],
                    fill=(200, 60, 60), outline=(120, 20, 20))

        # 夹爪（深灰两指）
        gy = _gripper_y(fi)
        gap = _jaw_gap(fi)
        for side in (-1, 1):
            jx = cx + side * gap / 2 - JAW_W / 2
            jy = gy - pen  # 坏片：指尖插入方块
            d.rectangle([jx, jy, jx + JAW_W, jy + JAW_H],
                        fill=(70, 70, 80), outline=(30, 30, 40))
        # 夹爪横梁
        d.rectangle([cx - gap / 2 - JAW_W, gy - 14,
                     cx + gap / 2 + JAW_W, gy - 8], fill=(70, 70, 80))

        # 中性帧标：不泄漏 kind/注入信息给 Rung4 裁判（真值只在 ground_truth.json）
        d.text((6, 6), f"frame {fi:02d}", fill=(60, 60, 70))
        frames.append(img)
    if kind == "good":
        defects: list[str] = []
        expected = {q: "yes" for q in
                    ("q1", "q2", "q3", "q4", "q5", "q6")}
    else:
        defects = [
            "geometry_overlap_penetration (frames 30-45, 6 px: block sinks "
            "into table and fingertips enter block)",
            "high_frequency_position_jitter (frames 30-50, ±4 px)"]
        # 逐题真值：q1/q5 穿模几何、q2/q4 抖动跳变、q3 夹持滑移；
        # q6 问末帧——注入止于 45/50，frame 59 为正常悬停，真值 yes。
        expected = {"q1": "no", "q2": "no", "q3": "no", "q4": "no",
                    "q5": "no", "q6": "yes"}
    meta = {
        "kind": kind, "fps": FPS, "n_frames": N_FRAMES,
        "contact_frame": CONTACT_FRAME,
        "injected_defects": defects,
        "expected_answers": expected,
        "answer_note": "逐题注入真值；bad 的 q6 末帧无注入，真值 yes。",
        "generator": "orchestrator/judge/clips.py (synthetic 2D calibration "
                     "fixture, NOT a simulator rollout)",
    }
    return frames, meta


def encode_mp4(frames, out_path: "str | Path", fps: int = FPS) -> Path:
    """ffmpeg rawvideo 管道编码 H.264 mp4（yuv420p，通用可播）。"""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{W}x{H}", "-r", str(fps), "-i", "-",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23",
           str(out)]
    proc = subprocess.run(cmd, input=b"".join(
        f.tobytes() for f in frames), capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败：{proc.stderr.decode(errors='replace')[-800:]}")
    # S-6：ffmpeg 直写路径无法走 tmp+replace，在产出点显式锁 0600。
    os.chmod(out, 0o600)
    return out


def write_clip_bundle(kind: str, dir_path: "str | Path") -> dict:
    """生成帧/mp4/ground_truth.json，返回 bundle 元信息。"""
    d = Path(dir_path)
    frames, meta = render_clip(kind)
    mp4 = encode_mp4(frames, d / f"{kind}.mp4")
    # S-6：ground_truth 原子写 0600 收口 fsutil（原裸 write_text 无 chmod）。
    atomic_write_text(d / f"{kind}.ground_truth.json",
                      json.dumps(meta, ensure_ascii=False, indent=2))
    return {"frames": frames, "mp4": str(mp4),
            "ground_truth": meta,
            "contact_frame": CONTACT_FRAME if kind == "good" else CONTACT_FRAME}
