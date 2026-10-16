#!/usr/bin/env python3
"""Task8 标定片合成：1 好片 + 1 注入穿地/抖动的坏片（侧视双足人形示意）。

与 orchestrator/judge/clips.py 同性质——**标定仪器**而非仿真产物：PIL 2D
示意动画（60 帧 @60fps），ground truth 由脚本注入，用于在 Task8 真实 batch
视频前检验步态 rubric 的检出力；残余风险（2D 卡通 ≠ Genesis 渲染、分屏）
写入 gait 标定件。

时序：双腿反相原地踏步（摆动相抬脚 14 px、前后 ±8 px），支撑相足面贴
地面线。坏片在 20-45 帧注入：支撑足下陷 6 px（穿地，只在触地相），躯干
与双足水平 ±4 px 高频抖动（固定种子，可复现）；46 帧起恢复，末帧干净
（q6 真值 yes）。

标定有效性纪律（同 clips.py）：帧内只印中性「frame NN」，不印 kind 或
INJECTED 标记；标准答案逐题注入，不做「坏片全 defect」映射。
"""
from __future__ import annotations

import json
from pathlib import Path

from .clips import FPS, H, N_FRAMES, W, encode_mp4

CONTACT_FRAME = 30
GROUND_Y = 180
HIP_X, HIP_Y = 160.0, 108.0
TORSO_TOP = 52
FOOT_W, FOOT_H = 16, 6
LIFT = 14.0
SWING_X = 8.0
SINK_PX = 6.0
INJECT_FRAMES = range(20, 46)


def _foot_pose(phase: float) -> tuple[float, float]:
    """phase∈[0,1)：前半摆动（抬脚+前移），后半支撑（贴地、回中）。"""
    import math
    if phase < 0.5:
        u = phase / 0.5
        lift = LIFT * math.sin(math.pi * u)
        x = SWING_X * (1.0 - math.cos(math.pi * u))
    else:
        u = (phase - 0.5) / 0.5
        lift = 0.0
        x = SWING_X * (1.0 + math.cos(math.pi * u)) - SWING_X
    return HIP_X + x, GROUND_Y - FOOT_H / 2 - lift


def render_gait_clip(kind: str):
    import random
    from PIL import Image, ImageDraw

    if kind not in ("good", "bad"):
        raise ValueError("kind 必须是 good|bad")
    rng = random.Random(20261015)          # 固定种子：坏片抖动可复现
    frames = []
    for fi in range(N_FRAMES):
        img = Image.new("RGB", (W, H), (245, 245, 248))
        d = ImageDraw.Draw(img)
        d.rectangle([0, GROUND_Y, W, H], fill=(180, 180, 190))
        d.line([0, GROUND_Y, W, GROUND_Y], fill=(80, 80, 90), width=2)

        phase = fi / N_FRAMES
        pr = phase % 1.0
        pl = (phase + 0.5) % 1.0
        fxr, fyr = _foot_pose(pr)
        fxl, fyl = _foot_pose(pl)

        # 坏片注入（20-45 帧）：躯干水平抖动 ±4 px；支撑相（phase≥.5）
        # 足下陷 6 px（穿地）。摆动相不注入——空中抬脚无「地面几何」可违。
        injected = kind == "bad" and fi in INJECT_FRAMES
        jitter_x = float(rng.choice([-4, -2, 3, 4, -3, 2])) if injected else 0.0
        sink_r = SINK_PX if (injected and pr >= 0.5) else 0.0
        sink_l = SINK_PX if (injected and pl >= 0.5) else 0.0
        hip_x = HIP_X + jitter_x

        for color, fx, fy, sink in (((60, 60, 120), fxr + jitter_x, fyr, sink_r),
                                   ((120, 60, 60), fxl + jitter_x, fyl, sink_l)):
            fy += sink
            knee_x = (hip_x + fx) / 2 + 4
            knee_y = (HIP_Y + fy) / 2 - 4
            d.line([hip_x, HIP_Y, knee_x, knee_y], fill=color, width=4)
            d.line([knee_x, knee_y, fx, fy], fill=color, width=4)
            d.rectangle([fx - FOOT_W / 2, fy - FOOT_H / 2,
                         fx + FOOT_W / 2, fy + FOOT_H / 2],
                        fill=color)

        # 躯干 + 头（随抖动整体水平平移）
        d.rectangle([hip_x - 9, TORSO_TOP, hip_x + 9, HIP_Y],
                    fill=(70, 70, 80))
        d.ellipse([hip_x - 8, TORSO_TOP - 18, hip_x + 8, TORSO_TOP - 2],
                  fill=(210, 180, 150), outline=(80, 60, 40))

        # 中性帧标：不泄漏 kind/注入信息给 Rung4 裁判（真值只在 ground_truth.json）
        d.text((6, 6), f"frame {fi:02d}", fill=(60, 60, 70))
        frames.append(img)

    if kind == "good":
        defects: list[str] = []
        expected = {f"q{i}": "yes" for i in range(1, 7)}
    else:
        defects = [
            f"stance_foot_ground_penetration (frames 20-45, stance foot "
            f"{SINK_PX:g}px below ground line)",
            "torso_high_frequency_jitter (frames 20-45, ±4px horizontal)"]
        # 逐题真值：q1 穿地、q2/q4 抖动；踏步行为仍可辨认（q3 yes）、
        # 轮廓未畸变（q5 yes）；注入止于 45，末帧 59 干净（q6 yes）。
        expected = {"q1": "no", "q2": "no", "q3": "yes", "q4": "no",
                    "q5": "yes", "q6": "yes"}
    meta = {
        "kind": kind, "fps": FPS, "n_frames": N_FRAMES,
        "contact_frame": CONTACT_FRAME,
        "injected_defects": defects,
        "expected_answers": expected,
        "answer_note": "逐题注入真值；bad 的 q3/q5/q6 无注入，真值 yes。",
        "generator": "orchestrator/judge/gait_clips.py (synthetic 2D calibration "
                     "fixture, NOT a Genesis rollout)",
    }
    return frames, meta


def write_gait_clip_bundle(kind: str, dir_path: "str | Path") -> dict:
    d = Path(dir_path)
    frames, meta = render_gait_clip(kind)
    mp4 = encode_mp4(frames, d / f"{kind}.mp4", fps=FPS)
    (d / f"{kind}.ground_truth.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"frames": frames, "mp4": str(mp4), "ground_truth": meta,
            "contact_frame": CONTACT_FRAME}
