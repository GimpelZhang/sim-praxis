"""Gate2 视频体积五级阶梯（详细计划 §1.4）纯函数 + 容器内 ffmpeg 命令生成。

阈值事实（2026-10-13 官方文档已查证）：
  - im/v1/files（file_type=mp4）硬上限 30MB；原片 ≤28MB 直接传（留 2MB 余量）；
  - step2 crf23 + yuv420p + 短边 720；
  - step3 crf28 + 短边 540 + fps20；
  - step4 裁 ≤30s 关键片段（保留首尾与接触时刻，区间由 Task7/8 调用方传入）；
  - step5 全部失败（含上传接口非 0）：内网链接降级，video_fallback=true，人工照常裁决。

宿主机不装 ffmpeg：压缩命令在 sim-sandbox 容器内执行（client 用
`docker compose -f sandbox/docker-compose.yml run --rm sim-sandbox ...` 调起）。
本模块纯函數不触网、不启容器，单测无需 mock ffmpeg 本身。
"""
from __future__ import annotations

from pathlib import Path

MB = 1024 * 1024
DIRECT_LIMIT = 28 * MB          # step1：原片 ≤28MB 直传
HARD_LIMIT = 30 * MB           # files 接口硬上限 30MB

# 初始阶梯的粗分档（只决定从哪一级开始尝试；逐级仍以压缩后实测体积为准）：
#   ≤28MB  -> 1 直传；≤64MB -> step2 通常足够；≤128MB -> step3；
#   ≤256MB -> step4 截断；更大体积 30s 截断也无把握 -> 直接 step5 链接降级。
_BAND_STEP2 = 64 * MB
_BAND_STEP3 = 128 * MB
_BAND_STEP4 = 256 * MB


def select_ladder_step(size_bytes: int) -> int:
    """按体积选初始压缩阶梯，返回 1..5。"""
    if size_bytes <= DIRECT_LIMIT:
        return 1
    if size_bytes <= _BAND_STEP2:
        return 2
    if size_bytes <= _BAND_STEP3:
        return 3
    if size_bytes <= _BAND_STEP4:
        return 4
    return 5


def next_ladder_step(step: int) -> int:
    """压缩/上传失败后推进一级（2->3->4->5；1 的失败由调用方按体积另判）。"""
    if step < 1 or step > 5:
        raise ValueError(f"非法阶梯: {step}")
    return min(step + 1, 5)


def _short_edge_scale(short_edge: int) -> str:
    # 横屏缩短边到 short_edge、竖屏同理；-2 保证宽高偶数（yuv420p 要求）
    return (f"scale='if(gt(iw,ih),-2,{short_edge})':"
            f"'if(gt(iw,ih),{short_edge},-2)'")


def compress_command(
    path: str,
    step: int,
    output_path: str,
    cover_path: str,
    *,
    trim_seconds: int = 30,
    trim_start: int = 0,
) -> str:
    """生成在 sim-sandbox 容器内 bash -lc 执行的 ffmpeg 命令字符串。

    每级都同时产出转码视频与首帧 PNG 封面（media 消息需要 image_key）。
    step1（直传）/step5（降级）无压缩语义，调用 ValueError——这两级不得调用本函数。
    step4 截断区间默认前 30s；真实关键片段区间（首尾+接触时刻）由 Task7/8 传入。
    """
    src = str(path)
    out = str(output_path)
    cover = str(cover_path)
    if step == 2:
        vf = _short_edge_scale(720)
        extra = ""
    elif step == 3:
        vf = f"fps=20,{_short_edge_scale(540)}"
        extra = ""
    elif step == 4:
        vf = f"fps=20,{_short_edge_scale(540)}"
        # -ss 放 -i 前快速 seek；trim_start/trim_seconds 由调用方按接触时刻给定
        extra = f" -ss {int(trim_start)} -t {int(trim_seconds)}"
    else:
        raise ValueError(f"step {step} 无容器内压缩命令（1=直传，5=内网降级）")
    return (
        f"ffmpeg -y{extra} -i {src} "
        f"-c:v libx264 -crf {23 if step == 2 else 28} -preset medium "
        f"-pix_fmt yuv420p -vf \"{vf}\" -c:a aac {out} && "
        f"ffmpeg -y -i {out} -frames:v 1 -an {cover}"
    )


def host_to_container_path(host_path: "str | Path", run_dir: "str | Path") -> str:
    """宿主 $SIMPRAXIS_RUN_DIR 路径 -> 容器内 /root/runs 挂载路径。

    非 RUN_DIR 下的产物无法被容器看到（compose 仅挂载该目录），直接报错而非隐式传错。
    """
    p = Path(host_path).resolve()
    root = Path(run_dir).resolve()
    try:
        rel = p.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"视频产物 {p} 不在 {root} 下，容器内不可见（compose 仅挂载 RUN_DIR）"
        ) from exc
    return str(Path("/root/runs") / rel)


def expected_outputs(path: "str | Path", step: int,
                     out_dir: "str | Path | None" = None) -> tuple[str, str]:
    """给定输入与阶梯，返回 (视频输出路径, 封面路径) 的宿主侧字面量。"""
    src = Path(path)
    d = Path(out_dir) if out_dir else src.parent
    stem = src.stem
    return (str(d / f"{stem}_s{step}.mp4"), str(d / f"{stem}_s{step}_cover.png"))
