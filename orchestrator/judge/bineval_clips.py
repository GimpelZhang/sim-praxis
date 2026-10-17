#!/usr/bin/env python3
"""Task4 BinEval 标定合成片（详细计划 L475、L491；12 片 ≥10，固定种子）。

锁内适配：计划命令行示意 ``python -m orchestrator.judge.clips --out ...``，
但 clips.py 只产 1 好 1 坏两片且无 main；BinEval 需要跨 gait/libero/fluid
三 track、逐题标签的多片集，故按 gait_calibrate.py 的「独立合成模块」范式
新建本模块（CLI：``python -m orchestrator.judge.bineval_clips --out DIR
--seed 20261016``）。

与 clips.py / gait_clips.py 同性质——**标定仪器**而非仿真产物：PIL 2D
示意动画（60 帧 @60fps），ground truth 由脚本逐片逐题注入；帧内只印中性
「frame NN」，不印 track/variant/INJECTED，真值只在 ground_truth.json，
不向 VLM 泄漏。12 片 = 3 track ×（1 全好 + 3 已知缺陷），缺陷片 9、好片 3。
active 条件（逐片 agreement≥0.7 且缺陷召回 100%）由 bineval.py fail-closed
机判；本模块只负责确定性合成，不产出任何模式结论。
"""
from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path

from orchestrator.common.fsutil import (
    atomic_write_bytes,
    atomic_write_text,
    ensure_private_dir,
)
from orchestrator.common.runtime_paths import phase2_root

from .clips import FPS, H, N_FRAMES, W, encode_mp4
from .collage import build_collage_png, select_frame_indices

DEFAULT_SEED = 20261016

GROUND_Y = 180

# 三片 track 的接触窗口中心帧（供六槽拼图选帧）。
CONTACT_FRAME = {"fluid": 30, "libero": 20, "gait": 30}


# --------------------------------------------------------------------------
# fluid track：发射杯 + 16 粒蓝粒子 + 右下接水杯
# --------------------------------------------------------------------------

FLUID_K = 16
_FLUID_BORN0, _FLUID_BORN_STEP = 10, 2
_FLUID_LIFE = 20
_X0, _Y0 = 60.0, 78.0
_X1, _Y1 = 232.0, 150.0
_CUP_X0, _CUP_X1, _CUP_BOTTOM = 210, 254, 178


def _fluid_particle(variant: str, fi: int, k: int):
    """返回单粒子 (x,y) 或 None（未出生/不可见）。"""
    born = _FLUID_BORN0 + _FLUID_BORN_STEP * k
    life = fi - born
    if life < 0:
        return None
    u = min(life / _FLUID_LIFE, 1.0)
    if variant in ("good", "teleport"):
        if u >= 1.0:
            # 落入杯内确定性堆叠
            row = k // 9
            return (216.0 + 4.0 * (k % 9) + 2.0 * row,
                    _CUP_BOTTOM - 3.0 - 5.0 * row)
        x = _X0 + (_X1 - _X0) * u
        y = _Y0 - 36.0 * math.sin(math.pi * u) + (_Y1 - _Y0) * u
        if variant == "teleport" and 30 <= fi <= 33 and life < _FLUID_LIFE:
            x += 46.0                    # 瞬移注入
        return x, y
    if variant == "spill":
        # 落入杯区后穿透杯底/地面继续下行（穿模 + 未入杯）
        if u < 1.0:
            x = _X0 + (_X1 - _X0) * u
            y = _Y0 - 36.0 * math.sin(math.pi * u) + (_Y1 - _Y0) * u
            return x, y
        v = min((life - _FLUID_LIFE) / 12.0, 1.0)
        return _X1 + 18.0 * v, _Y1 + 64.0 * v
    # wrongdir：离开发射杯后逆重力向上偏左飞，全部落不进杯
    return _X0 - 64.0 * u, _Y0 - 62.0 * u


def _draw_fluid(variant: str, frames: list) -> None:
    from PIL import Image, ImageDraw
    for fi in range(N_FRAMES):
        img = frames[fi]
        d = ImageDraw.Draw(img)
        # 发射杯（fi>=10 起倾转）
        tilt = 0.5 if fi >= 10 else 0.0
        bx, by = 52, 74
        d.polygon([(bx - 12, by - 8), (bx + 12, by - 8),
                   (bx + 12 - 10 * tilt, by + 8),
                   (bx - 12 - 10 * tilt, by + 8)],
                  fill=(90, 90, 110), outline=(40, 40, 60))
        # 接水杯（U 形深绿壁）
        d.line([(_CUP_X0, 132), (_CUP_X0, _CUP_BOTTOM)], fill=(20, 110, 60),
               width=3)
        d.line([(_CUP_X1, 132), (_CUP_X1, _CUP_BOTTOM)], fill=(20, 110, 60),
               width=3)
        d.line([(_CUP_X0, _CUP_BOTTOM), (_CUP_X1, _CUP_BOTTOM)],
               fill=(20, 110, 60), width=3)
        for k in range(FLUID_K):
            p = _fluid_particle(variant, fi, k)
            if p is None:
                continue
            x, y = p
            if 0 <= x <= W and 0 <= y <= H:
                d.ellipse([x - 2.5, y - 2.5, x + 2.5, y + 2.5],
                          fill=(40, 110, 220))


# --------------------------------------------------------------------------
# libero track：夹爪推黄色方块到右侧目标区
# --------------------------------------------------------------------------

_BLOCK_X0, _BLOCK_X1, _BLOCK_SIZE = 104.0, 250.0, 28.0


def _libero_block_cx(variant: str, fi: int):
    if variant == "airpush":
        # 夹爪仍在上方下降时方块即自行滑向目标（隔空受力）
        if fi < 8:
            return _BLOCK_X0
        if fi <= 37:
            return _BLOCK_X0 + (_BLOCK_X1 - _BLOCK_X0) * (fi - 8) / 29
        return _BLOCK_X1
    # good / wrongdir / vanish 的接触时刻同为 15
    u = min(max((fi - 15) / 29.0, 0.0), 1.0)
    if variant == "wrongdir":
        return _BLOCK_X0 - 74.0 * u
    return _BLOCK_X0 + (_BLOCK_X1 - _BLOCK_X0) * u


def _draw_libero(variant: str, frames: list) -> None:
    from PIL import Image, ImageDraw
    for fi in range(N_FRAMES):
        img = frames[fi]
        d = ImageDraw.Draw(img)
        # 目标区（地面虚线框）
        for x in range(232, 272, 6):
            d.line([(x, GROUND_Y), (x + 3, GROUND_Y)], fill=(190, 130, 20),
                   width=2)
        d.rectangle([232, GROUND_Y - 40, 272, GROUND_Y],
                    outline=(190, 130, 20))
        # 夹爪（深灰两指 + 横梁）
        if variant == "airpush":
            gx = _BLOCK_X0
            gy = 60.0 + 74.0 * min(fi / 24.0, 1.0)
        else:
            u = min(max((fi - 15) / 29.0, 0.0), 1.0)
            gx = _libero_block_cx("good", fi) if fi >= 15 else _BLOCK_X0
            if fi < 15:
                gy = 60.0 + 74.0 * fi / 14.0
            elif fi <= 44:
                gy = GROUND_Y - _BLOCK_SIZE - 4.0
            else:
                gy = max(60.0, GROUND_Y - _BLOCK_SIZE - 4.0
                         - 74.0 * (fi - 44) / 15.0)
        gap = _BLOCK_SIZE + 12.0
        for side in (-1, 1):
            jx = gx + side * (gap / 2 + 2.0) - 4.0
            d.rectangle([jx, gy, jx + 8, gy + 30],
                        fill=(70, 70, 80), outline=(30, 30, 40))
        d.rectangle([gx - gap / 2 - 6, gy - 8, gx + gap / 2 + 6, gy - 2],
                    fill=(70, 70, 80))
        # 方块（vanish：34 帧起不画——凭空消失且未到目标）
        if variant == "vanish" and fi >= 34:
            continue
        cx = _libero_block_cx(variant, fi)
        by = GROUND_Y - _BLOCK_SIZE / 2
        d.rectangle([cx - _BLOCK_SIZE / 2, by - _BLOCK_SIZE / 2,
                     cx + _BLOCK_SIZE / 2, by + _BLOCK_SIZE / 2],
                    fill=(225, 190, 50), outline=(140, 110, 10))


# --------------------------------------------------------------------------
# gait track：简笔人形走向右侧旗帜
# ----------------------------------------------------------------------------

_HIP_Y = 108.0
_FLAG_X = 262


def _gait_hip_x(variant: str, fi: int) -> float:
    if fi < 5:
        return 60.0
    if fi >= 50:
        return 250.0
    x = 60.0 + 190.0 * (fi - 5) / 45.0
    if variant == "jump" and fi >= 30:
        x = min(x + 46.0, 250.0)     # 第 30 帧起永久前移 46px（瞬移）
    return x


def _draw_gait(variant: str, frames: list) -> None:
    from PIL import Image, ImageDraw
    for fi in range(N_FRAMES):
        img = frames[fi]
        d = ImageDraw.Draw(img)
        # 旗帜/目标标记
        d.line([(_FLAG_X, GROUND_Y), (_FLAG_X, 60)], fill=(120, 60, 60),
               width=2)
        d.polygon([(_FLAG_X, 60), (_FLAG_X + 18, 67), (_FLAG_X, 74)],
                  fill=(200, 70, 70))
        hip_x = _gait_hip_x(variant, fi)
        walking = 5 <= fi < 50
        ph = ((fi - 5) % 14) / 14 if walking else 0.0
        feet = []
        for leg in (0, 1):
            p = (ph + leg * 0.5) % 1.0
            if walking and p < 0.5:
                lift = 10.0 * math.sin(math.pi * (p / 0.5))
                dx = 9.0 * math.sin(2 * math.pi * p)
            else:
                lift, dx = 0.0, 9.0 if leg == 0 else -9.0
            feet.append((hip_x + dx, GROUND_Y - lift))
        if variant == "sink" and 20 <= fi <= 45:
            feet = [(x, y + 6.0) for x, y in feet]      # 支撑相穿地
        if variant == "airstep" and 15 <= fi <= 35:
            feet = [(x, y - 14.0) for x, y in feet]     # 双脚腾空仍前进
        for fx, fy in feet:
            d.line([(hip_x, _HIP_Y), (fx, fy)], fill=(40, 40, 50), width=3)
        d.line([(hip_x, _HIP_Y), (hip_x, _HIP_Y - 40)],
               fill=(40, 40, 50), width=3)
        d.ellipse([hip_x - 7, _HIP_Y - 55, hip_x + 7, _HIP_Y - 41],
                  fill=(60, 60, 70))


# --------------------------------------------------------------------------

def _labels(track: str, variant: str) -> dict[str, str]:
    yes = {q: "yes" for q in ("q1", "q2", "q3", "q4", "q5")}
    if variant == "good":
        return dict(yes)
    table = {
        ("fluid", "spill"): {"q2": "no", "q4": "no"},
        ("fluid", "teleport"): {"q5": "no"},
        ("fluid", "wrongdir"): {"q3": "no", "q4": "no"},
        ("libero", "airpush"): {"q1": "no"},
        ("libero", "wrongdir"): {"q3": "no", "q4": "no"},
        ("libero", "vanish"): {"q4": "no", "q5": "no"},
        ("gait", "sink"): {"q2": "no"},
        ("gait", "jump"): {"q5": "no"},
        ("gait", "airstep"): {"q1": "no"},
    }
    out = dict(yes)
    out.update(table[(track, variant)])
    return out


_DEFECT_NOTE = {
    ("fluid", "spill"): "particles pass through cup wall and floor",
    ("fluid", "teleport"): "in-flight particles jump +46px frames 30-33",
    ("fluid", "wrongdir"): "particles fly up-left against gravity, miss cup",
    ("libero", "airpush"): "block slides to target while gripper still "
                           "descending, no contact",
    ("libero", "wrongdir"): "block moves left opposite push direction",
    ("libero", "vanish"): "block vanishes at frame 34, target unreached",
    ("gait", "sink"): "feet sink 6px below ground line frames 20-45",
    ("gait", "jump"): "figure teleports +46px at frame 30",
    ("gait", "airstep"): "body advances while both feet are airborne 15-35",
}

CATALOG: tuple[tuple[str, str, str], ...] = tuple(
    (track, variant)
    for track in ("fluid", "libero", "gait")
    for variant in ("good",) + {
        "fluid": ("spill", "teleport", "wrongdir"),
        "libero": ("airpush", "wrongdir", "vanish"),
        "gait": ("sink", "jump", "airstep"),
    }[track]
)


def render_clip(track: str, variant: str, seed: int = DEFAULT_SEED):
    """返回 (frames[PIL.Image], ground_truth_meta)。"""
    from PIL import Image
    if (track, variant) not in CATALOG:
        raise ValueError(f"未知 clip：track={track} variant={variant}")
    # 仅用于确定性消费预留；当前注入全部由帧号决定（与 clips.py 同口径）。
    random.Random(seed + hash((track, variant)) % 100000)
    frames = [Image.new("RGB", (W, H), (245, 245, 248))
              for _ in range(N_FRAMES)]
    # 共用地面线（三个场景都在地面/杯底高度表达接触）。
    from PIL import ImageDraw
    for img in frames:
        ImageDraw.Draw(img).rectangle([0, GROUND_Y, W, H],
                                      fill=(180, 180, 190))
        ImageDraw.Draw(img).line([0, GROUND_Y, W, GROUND_Y],
                                 fill=(80, 80, 90), width=2)
    {"fluid": _draw_fluid, "libero": _draw_libero,
     "gait": _draw_gait}[track](variant, frames)
    for fi, img in enumerate(frames):
        # 中性帧标：不泄漏 track/variant/真值（真值只在 ground_truth.json）。
        ImageDraw.Draw(img).text((6, 6), f"frame {fi:02d}",
                                 fill=(60, 60, 70))
    labels = _labels(track, variant)
    defects = ([] if variant == "good"
               else [_DEFECT_NOTE[(track, variant)]])
    meta = {
        "track": track,
        "variant": variant,
        "fps": FPS,
        "n_frames": N_FRAMES,
        "contact_frame": CONTACT_FRAME[track],
        "injected_defects": defects,
        "labels": labels,
        "generator": "orchestrator/judge/bineval_clips.py (synthetic 2D "
                     "calibration fixture, NOT a simulator rollout)",
    }
    return frames, meta


def prepare(out_dir: "str | Path | None" = None,
            seed: int = DEFAULT_SEED) -> dict:
    """生成 12 片（video.mp4 + bineval_collage.png + ground_truth.json）。

    产物只落 /mnt（默认 $SIMPRAXIS_PHASE2_ROOT/task4_judge/synth）；
    目录 0700、文件 0600（mp4 在 encode_mp4 产出点锁，其余走 fsutil）。
    """
    base = (Path(out_dir) if out_dir is not None
            else phase2_root() / "task4_judge" / "synth")
    ensure_private_dir(base)
    clips_info = []
    for i, (track, variant) in enumerate(CATALOG):
        name = f"clip_{i + 1:02d}_{track}_{variant}"
        d = base / name
        ensure_private_dir(d)
        frames, meta = render_clip(track, variant, seed=seed)
        mp4 = encode_mp4(frames, d / "video.mp4", fps=FPS)
        contact = CONTACT_FRAME[track]
        idx = select_frame_indices(N_FRAMES, contact)
        labels = [f"frame {j:02d}" for j in idx]
        png = d / "bineval_collage.png"
        atomic_write_bytes(png,
                           build_collage_png([frames[j] for j in idx], labels))
        gt_path = d / "ground_truth.json"
        atomic_write_text(gt_path,
                          json.dumps(meta, ensure_ascii=False, indent=2))
        clips_info.append({"clip": name, "track": track,
                           "variant": variant, "video": str(mp4),
                           "collage": str(png), "ground_truth": str(gt_path),
                           "labels": meta["labels"]})
    manifest = {
        "seed": seed,
        "fps": FPS,
        "n_frames": N_FRAMES,
        "n_clips": len(clips_info),
        "clips": clips_info,
        "generator": "orchestrator/judge/bineval_clips.py",
    }
    atomic_write_text(base / "manifest.json",
                      json.dumps(manifest, ensure_ascii=False, indent=2))
    return {"dir": str(base), "manifest": manifest}


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(
        description="Task4 BinEval 合成标定片（0 付费，PIL 2D 示意动画）")
    ap.add_argument("--out", default=None,
                    help="默认 $SIMPRAXIS_PHASE2_ROOT/task4_judge/synth")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = ap.parse_args(argv)
    info = prepare(args.out, seed=args.seed)
    print(json.dumps({"dir": info["dir"],
                      "n_clips": info["manifest"]["n_clips"],
                      "seed": args.seed}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
