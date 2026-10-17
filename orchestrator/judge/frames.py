#!/usr/bin/env python3
"""Task4 frames 管线：容器内 ffmpeg 命令纯函数生成（详细计划 L464）。

管线契约（与 Phase1 2×3 collage **并行**，不替换老拼图——老 collage 继续
作 Gate2 证据，本管线产物供 BinEval VLM）：
  10 fps 抽帧 → 短边 512 等比缩放 → contact-window 六帧拼单张 JPEG，
  -q:v 分档降级使文件 ≤100KB。

纪律（对齐 orchestrator/feishu_gateway/video.py）：
- 本模块**只生成命令字符串**，纯函数、不触网、不启容器、宿主机绝不执行
  ffmpeg；命令由后续容器侧 runner（Task7/8 接缝以外）按序执行。
- S-9：输入/输出文件名前导横线在 shlex.quote 前加 ``./``，防 ffmpeg 选项注入。
- S-3：数值参数 NaN/Inf 显式拒绝，不流进 -ss/-t/-q:v。
- 体积守卫只生成 ``test $(stat -c%s ...) -le N`` 参数字符串，同样不在宿主机执行。
"""
from __future__ import annotations

import math
import shlex
from pathlib import Path
from typing import Sequence

# 10 fps 抽帧（计划 L464 钉死）。
FRAME_FPS = 10
# 短边 512 等比缩放（计划 L464 钉死）。
SHORT_EDGE = 512
# contact-window 六帧（与 collage.select_frame_indices 六槽语义一致），3×2。
WINDOW_FRAMES = 6
TILE_LAYOUT = "3x2"
# 六帧 @10fps = 0.6s 窗口；接触时刻居中（前 0.3s / 后 0.3s）。
WINDOW_SECONDS = WINDOW_FRAMES / FRAME_FPS
# JPEG 体积硬目标：十进制 KB 口径（对齐 video.py H-14 的 MB 口径）。
JPEG_MAX_BYTES = 100_000
# -q:v 分档（JPEG：2 质量最高/最大，31 最差/最小）；容器侧按序降级直到
# 体积守卫通过。首档 2 给最清晰判定件，末档 20 对 3×2、短边 512 拼图足够。
JPEG_QUALITY_TIERS: tuple[int, ...] = (2, 6, 10, 15, 20)
_JPEG_Q_MIN, _JPEG_Q_MAX = 2, 31


def _q(p: "str | Path") -> str:
    """S-9：前导横线文件名经 shlex.quote 后仍是 "-x.mp4"，会被 ffmpeg 当
    选项；统一在 quote 之前加 ./ 前缀（与 video.py 同口径）。"""
    s = str(p)
    return shlex.quote("./" + s if s.startswith("-") else s)


def _finite(name: str, value: float) -> float:
    f = float(value)
    if not math.isfinite(f):
        raise ValueError(f"{name} 必须为有限数值，收到 {value!r}")
    return f


def _short_edge_scale(short_edge: int = SHORT_EDGE) -> str:
    # 横屏缩短边到 short_edge、竖屏同理；-2 保证宽高偶数（yuv420p 要求）。
    return (f"scale='if(gt(iw,ih),-2,{short_edge})':"
            f"'if(gt(iw,ih),{short_edge},-2)'")


def contact_window_start(contact_moment_s: "float | None",
                         window_s: float = WINDOW_SECONDS) -> float:
    """接触时刻居中的窗口起点；无接触时刻（分屏无单一接触）时取片头。"""
    if contact_moment_s is None:
        return 0.0
    c = _finite("contact_moment_s", contact_moment_s)
    if c < 0:
        raise ValueError(f"contact_moment_s 不得为负：{c}")
    w = _finite("window_s", window_s)
    if w <= 0:
        raise ValueError(f"window_s 必须为正：{w}")
    return max(0.0, c - w / 2)


def contact_window_command(
    video_path: "str | Path",
    output_path: "str | Path",
    *,
    contact_moment_s: "float | None" = None,
    window_s: float = WINDOW_SECONDS,
    fps: int = FRAME_FPS,
    short_edge: int = SHORT_EDGE,
    quality: int = JPEG_QUALITY_TIERS[0],
) -> str:
    """生成单条 ffmpeg 命令：-ss 窗口 → 10fps 抽帧 → 短边缩放 → 3×2 tile
    → 单张 JPEG（-frames:v 1）。纯函数，不执行。

    quality 取 -q:v（2..31）；调用方按 JPEG_QUALITY_TIERS 顺序降级。
    """
    if not isinstance(fps, int) or isinstance(fps, bool) or fps <= 0:
        raise ValueError(f"fps 必须为正整数，收到 {fps!r}")
    if (not isinstance(short_edge, int) or isinstance(short_edge, bool)
            or short_edge <= 0):
        raise ValueError(f"short_edge 必须为正整数，收到 {short_edge!r}")
    if (not isinstance(quality, int) or isinstance(quality, bool)
            or not _JPEG_Q_MIN <= quality <= _JPEG_Q_MAX):
        raise ValueError(
            f"quality(-q:v) 必须为 {_JPEG_Q_MIN}..{_JPEG_Q_MAX} 整数，"
            f"收到 {quality!r}")
    start = contact_window_start(contact_moment_s, window_s)
    w = _finite("window_s", window_s)
    vf = f"fps={fps},{_short_edge_scale(short_edge)},tile={TILE_LAYOUT}"
    return (
        f"ffmpeg -y -ss {start:.3f} -t {w:.3f} -i {_q(video_path)} "
        f"-vf \"{vf}\" -frames:v 1 -q:v {int(quality)} {_q(output_path)}"
    )


def size_guard_command(output_path: "str | Path",
                       limit: int = JPEG_MAX_BYTES) -> str:
    """生成体积守卫命令字符串（stat 十进制字节数 ≤ limit 即通过）。

    仅生成参数，宿主机绝不执行；容器侧 runner 用其退出码判断是否降档重编码。
    """
    if (not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0):
        raise ValueError(f"limit 必须为正整数，收到 {limit!r}")
    return f"test \"$(stat -c%s {_q(output_path)})\" -le {int(limit)}"


def contact_window_plan(
    video_path: "str | Path",
    out_dir: "str | Path | None" = None,
    *,
    contact_moment_s: "float | None" = None,
    stem: "str | None" = None,
    qualities: Sequence[int] = JPEG_QUALITY_TIERS,
) -> dict:
    """生成完整的分档命令契约（Task7/8 evaluate 接缝并行挂接，不实跑）。

    返回 dict：
      {
        "video": 输入视频字面量,
        "fps": 10, "short_edge": 512, "tile": "3x2",
        "window_seconds": 0.6, "jpeg_max_bytes": 100000,
        "output_jpeg": 产物路径（各档覆写同一路径）,
        "executed": False,        # 契约：本模块/宿主绝不执行
        "steps": [{"tier", "quality", "command", "guard"}, ...],
      }
    容器侧执行约定：按 steps 顺序运行 command，随后跑 guard；首个 guard
    退出码 0 即止；全部失败则该 clip 视觉判定走 VLM 错误→升级人工（fail-closed）。
    """
    src = Path(str(video_path))
    d = Path(str(out_dir)) if out_dir is not None else src.parent
    name = stem if stem is not None else f"{src.stem}_contact_window"
    output = d / f"{name}.jpg"
    tiers = tuple(qualities)
    if not tiers:
        raise ValueError("qualities 至少需要一个分档")
    steps = []
    for tier, qv in enumerate(tiers, start=1):
        steps.append({
            "tier": tier,
            "quality": int(qv),
            "command": contact_window_command(
                src, output, contact_moment_s=contact_moment_s,
                quality=int(qv)),
            "guard": size_guard_command(output),
        })
    return {
        "video": str(src),
        "fps": FRAME_FPS,
        "short_edge": SHORT_EDGE,
        "tile": TILE_LAYOUT,
        "window_seconds": WINDOW_SECONDS,
        "contact_moment_s": (None if contact_moment_s is None
                             else float(contact_moment_s)),
        "jpeg_max_bytes": JPEG_MAX_BYTES,
        "output_jpeg": str(output),
        "executed": False,
        "steps": steps,
    }
