#!/usr/bin/env python3
"""Rung4 抽帧拼图（详细计划 L525）：首/中/末 + 接触时刻 ±5 帧，2×3 PNG。

帧源（Task5 标定）：合成器直接给 PIL 帧列表；Task7 起从 mp4 抽帧（imageio
在容器侧 pin imageio[ffmpeg]==2.36.1，宿主未装则延迟报错，不在此安装）。
"""
from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Sequence

CONTACT_WING = 5          # 接触时刻前后各 5 帧
GRID_COLS, GRID_ROWS = 3, 2
CELL_W, CELL_H = 320, 240
LABEL_H = 18


def select_frame_indices(n_frames: int, contact_index: "int | None") -> list[int]:
    """六个槽位：首 / 接触-5 / 接触 / 接触+5 / 中 / 末（排序去重，不足补齐）。

    无接触帧（接触未发生）时用中间帧代替接触中心，保证 2×3 满格。
    """
    if n_frames <= 0:
        raise ValueError("帧数必须为正")
    first, mid, last = 0, n_frames // 2, n_frames - 1
    c = contact_index if contact_index is not None else mid
    c = max(0, min(n_frames - 1, int(c)))
    wanted = [first, c - CONTACT_WING, c, c + CONTACT_WING, mid, last]
    clamped = sorted({max(0, min(n_frames - 1, x)) for x in wanted})
    # 去重后不足 6 格：在区间内均匀补帧（仍不足则允许重复末帧，满格优先）
    fill = 0
    while len(clamped) < GRID_COLS * GRID_ROWS:
        cand = int(round(fill * (n_frames - 1)
                         / (GRID_COLS * GRID_ROWS - 1)))
        if cand not in clamped:
            clamped.append(cand)
            clamped.sort()
        fill += 1
        if fill > 100:  # 极端短帧兜底
            clamped.append(last)
            clamped.sort()
    return clamped[:GRID_COLS * GRID_ROWS]


def build_collage_png(frames: "Sequence['PILImage']",
                      labels: "Sequence[str] | None" = None) -> bytes:
    """把 6 张 PIL 帧拼成 2×3 PNG（统一缩放、黑底、帧序标签）。"""
    from PIL import Image, ImageDraw

    n = GRID_COLS * GRID_ROWS
    if len(frames) < n:
        raise ValueError(f"需要至少 {n} 帧，收到 {len(frames)}")
    W = GRID_COLS * CELL_W
    H = GRID_ROWS * (CELL_H + LABEL_H)
    canvas = Image.new("RGB", (W, H), (12, 12, 12))
    draw = ImageDraw.Draw(canvas)
    for slot, frame in enumerate(frames[:n]):
        im = frame.convert("RGB")
        im = im.resize((CELL_W, CELL_H))
        x = (slot % GRID_COLS) * CELL_W
        y = (slot // GRID_COLS) * (CELL_H + LABEL_H)
        canvas.paste(im, (x, y + LABEL_H))
        tag = labels[slot] if labels else f"frame slot {slot + 1}/6"
        draw.rectangle([x, y, x + CELL_W, y + LABEL_H], fill=(0, 0, 0))
        draw.text((x + 6, y + 3), tag, fill=(230, 230, 230))
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


def collage_b64(frames: "Sequence['PILImage']",
                labels: "Sequence[str] | None" = None) -> str:
    return base64.b64encode(build_collage_png(frames, labels)).decode("ascii")


def extract_frames_mp4(path: "str | Path") -> "list['PILImage']":
    """Task7 容器路径：imageio[ffmpeg] 抽全部帧（宿主无 imageio 时显式报错）。"""
    try:
        import imageio.v3 as iio  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "mp4 抽帧需要 imageio[ffmpeg]（容器内 pin 2.36.1）；"
            "Task5 标定走合成 PIL 帧，不经过此路径") from exc
    return [_arr_to_pil(a) for a in iio.imiter(path)]


def _arr_to_pil(arr):
    from PIL import Image
    return Image.fromarray(arr)
